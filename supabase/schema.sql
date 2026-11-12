-- FantasyBet: saldos, mercados y boletos con dinero de juego.
-- El navegador solo entra por las funciones; las tablas no se leen directo.
create extension if not exists pgcrypto;

create table if not exists bettors (
  team_id   int primary key,
  name      text not null,
  pin_hash  text not null,
  balance   numeric(12,2) not null default 10000,
  created_at timestamptz not null default now()
);

create table if not exists markets (
  id         text primary key,
  week       int  not null,
  grp        text not null,
  label      text not null,
  dec        numeric(8,3) not null,
  open_dec   numeric(8,3) not null,
  status     text not null default 'open',   -- open | suspended | closed
  result     text,                           -- win | loss | push
  updated_at timestamptz not null default now()
);

create table if not exists bets (
  id         bigserial primary key,
  team_id    int not null references bettors(team_id),
  stake      numeric(12,2) not null,
  dec        numeric(12,3) not null,
  status     text not null default 'open',   -- open | won | lost | push
  payout     numeric(14,2),
  created_at timestamptz not null default now(),
  settled_at timestamptz
);

create table if not exists bet_legs (
  bet_id    bigint not null references bets(id) on delete cascade,
  market_id text   not null references markets(id),
  label     text   not null,
  dec       numeric(8,3) not null,
  primary key (bet_id, market_id)
);

alter table bettors  enable row level security;
alter table markets  enable row level security;
alter table bets     enable row level security;
alter table bet_legs enable row level security;

create or replace function _auth(p_team int, p_pin text) returns bettors
language plpgsql security definer set search_path = public, extensions as $$
declare b bettors;
begin
  select * into b from bettors where team_id = p_team;
  if not found or b.pin_hash <> crypt(p_pin, b.pin_hash) then
    raise exception 'PIN incorrecto';
  end if;
  return b;
end $$;

-- Primer ingreso de un equipo: fija su PIN y le da el presupuesto inicial.
create or replace function claim(p_team int, p_name text, p_pin text) returns json
language plpgsql security definer set search_path = public, extensions as $$
declare b bettors;
begin
  if p_pin !~ '^[0-9]{4,6}$' then raise exception 'El PIN debe tener de 4 a 6 números'; end if;
  if exists (select 1 from bettors where team_id = p_team) then
    b := _auth(p_team, p_pin);
  else
    insert into bettors(team_id, name, pin_hash) values (p_team, p_name, crypt(p_pin, gen_salt('bf')))
    returning * into b;
  end if;
  return json_build_object('team_id', b.team_id, 'balance', b.balance);
end $$;

create or replace function place_bet(p_team int, p_pin text, p_markets text[], p_stake numeric, p_expected numeric)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare b bettors; total numeric := 1; n int; bad text; new_id bigint;
begin
  b := _auth(p_team, p_pin);
  n := coalesce(array_length(p_markets, 1), 0);
  if n < 1 or n > 12 then raise exception 'El boleto debe tener de 1 a 12 selecciones'; end if;
  if p_stake < 10 then raise exception 'La apuesta mínima es $10'; end if;
  if p_stake > b.balance then raise exception 'Saldo insuficiente'; end if;
  if (select count(*) from markets where id = any(p_markets)) <> n then
    raise exception 'Alguna selección ya no existe; recarga la página';
  end if;
  select label into bad from markets where id = any(p_markets) and status <> 'open' limit 1;
  if bad is not null then raise exception 'Ya no se aceptan apuestas en: %', bad; end if;
  if (select count(distinct grp) from markets where id = any(p_markets)) <> n then
    raise exception 'Solo una selección por jugador y una por mercado de cada matchup';
  end if;
  select exp(sum(ln(dec))) into total from markets where id = any(p_markets);
  total := round(total, 3);
  if abs(total - p_expected) / p_expected > 0.002 then
    raise exception 'Las líneas cambiaron (ahora %). Revisa tu boleto', total;
  end if;
  if p_stake * total > 1000000 then raise exception 'El pago máximo por boleto es $1,000,000'; end if;
  update bettors set balance = balance - p_stake where team_id = p_team;
  insert into bets(team_id, stake, dec) values (p_team, p_stake, total) returning id into new_id;
  insert into bet_legs(bet_id, market_id, label, dec)
    select new_id, id, label, dec from markets where id = any(p_markets);
  return json_build_object('bet_id', new_id, 'dec', total, 'balance', b.balance - p_stake);
end $$;

create or replace function my_bets(p_team int, p_pin text) returns json
language plpgsql security definer set search_path = public, extensions as $$
declare b bettors;
begin
  b := _auth(p_team, p_pin);
  return json_build_object('balance', b.balance, 'bets', coalesce((
    select json_agg(x order by x.created_at desc) from (
      select bt.id, bt.stake, bt.dec, bt.status, bt.payout, bt.created_at,
        (select json_agg(json_build_object('label', l.label, 'dec', l.dec, 'result', m.result))
           from bet_legs l join markets m on m.id = l.market_id where l.bet_id = bt.id) legs
      from bets bt where bt.team_id = p_team limit 200) x), '[]'::json));
end $$;

-- Tabla pública: saldos, resultado de la casa y últimos boletos de la liga.
create or replace function board() returns json
language sql security definer set search_path = public as $$
  select json_build_object(
    'bettors', coalesce((select json_agg(json_build_object(
        'team_id', b.team_id, 'name', b.name, 'balance', b.balance,
        'at_risk', coalesce((select sum(stake) from bets where team_id = b.team_id and status = 'open'), 0),
        'won', (select count(*) from bets where team_id = b.team_id and status = 'won'),
        'lost', (select count(*) from bets where team_id = b.team_id and status = 'lost'))
      order by b.balance desc) from bettors b), '[]'::json),
    'house', json_build_object(
        'handle', coalesce((select sum(stake) from bets), 0),
        'profit', coalesce((select sum(stake - coalesce(payout, 0)) from bets where status <> 'open'), 0),
        'open', coalesce((select sum(stake) from bets where status = 'open'), 0)),
    'recent', coalesce((select json_agg(x) from (
        select bt.id, b.name, bt.stake, bt.dec, bt.status, bt.payout, bt.created_at,
          (select json_agg(l.label) from bet_legs l where l.bet_id = bt.id) legs
        from bets bt join bettors b using (team_id) order by bt.created_at desc limit 40) x), '[]'::json));
$$;

-- ---- Solo para el proceso automático (service_role) ----

create or replace function sync_markets(rows jsonb) returns int
language sql security definer set search_path = public as $$
  with r as (
    insert into markets(id, week, grp, label, dec, open_dec, status, result, updated_at)
    select x->>'id', (x->>'week')::int, x->>'grp', x->>'label', (x->>'dec')::numeric, (x->>'dec')::numeric,
           x->>'status', nullif(x->>'result', ''), now()
    from jsonb_array_elements(rows) x
    on conflict (id) do update set
      label = excluded.label, grp = excluded.grp,
      dec = case when markets.result is null then excluded.dec else markets.dec end,
      status = excluded.status,
      result = coalesce(excluded.result, markets.result),
      updated_at = now()
    returning 1)
  select count(*)::int from r;
$$;

create or replace function sync_results(rows jsonb) returns int
language sql security definer set search_path = public as $$
  with r as (
    update markets m set result = x->>'result', status = 'closed', updated_at = now()
    from jsonb_array_elements(rows) x where m.id = x->>'id' and m.result is null returning 1)
  select count(*)::int from r;
$$;

-- Semanas con selecciones apostadas que todavía no tienen resultado.
create or replace function open_weeks() returns setof int
language sql security definer set search_path = public as $$
  select distinct m.week from bet_legs l join bets b on b.id = l.bet_id join markets m on m.id = l.market_id
  where b.status = 'open' and m.result is null and m.week > 0;
$$;

-- Mercados que desaparecen (jugador cambiado de alineación, etc.) se cierran si nadie los liquidó.
create or replace function settle_bets() returns int
language plpgsql security definer set search_path = public as $$
declare r record; n int := 0; pay numeric;
begin
  for r in
    select b.id, b.team_id, b.stake,
      bool_or(m.result = 'loss') any_loss,
      bool_and(m.result is not null) all_done,
      bool_and(m.result = 'push') all_push,
      exp(sum(ln(case when m.result = 'win' then l.dec else 1 end))) mult
    from bets b join bet_legs l on l.bet_id = b.id join markets m on m.id = l.market_id
    where b.status = 'open' group by b.id
  loop
    if r.any_loss then
      update bets set status = 'lost', payout = 0, settled_at = now() where id = r.id;
      n := n + 1;
    elsif r.all_done then
      pay := round(r.stake * r.mult, 2);
      update bets set status = case when r.all_push then 'push' else 'won' end,
        payout = pay, settled_at = now() where id = r.id;
      update bettors set balance = balance + pay where team_id = r.team_id;
      n := n + 1;
    end if;
  end loop;
  return n;
end $$;

revoke all on function _auth(int, text), sync_markets(jsonb), sync_results(jsonb), open_weeks(), settle_bets()
  from public, anon, authenticated;
grant execute on function claim(int, text, text), place_bet(int, text, text[], numeric, numeric),
  my_bets(int, text), board() to anon, authenticated;

-- Lo que no se regeneró en esta corrida: se cierra; en semanas pasadas se anula (push).
create or replace function close_stale(p_week int, p_since timestamptz) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update markets set status = 'closed'
    where updated_at < p_since and status <> 'closed' and week in (0, p_week);
  update markets set status = 'closed', result = 'push'
    where updated_at < p_since and result is null and week > 0 and week < p_week;
  get diagnostics n = row_count;
  return n;
end $$;
revoke all on function close_stale(int, timestamptz) from public, anon, authenticated;
