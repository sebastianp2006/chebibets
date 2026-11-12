-- Parte 2: funciones de promociones
create or replace function sync_markets(rows jsonb) returns int
language sql security definer set search_path = public as $$
  with r as (
    insert into markets(id, week, grp, label, dec, open_dec, status, result, max_stake, was_dec, legs, updated_at)
    select x->>'id', (x->>'week')::int, x->>'grp', x->>'label', (x->>'dec')::numeric, (x->>'dec')::numeric,
           x->>'status', nullif(x->>'result', ''), (x->>'max_stake')::numeric, (x->>'was_dec')::numeric,
           case when jsonb_typeof(x->'legs') = 'array' then x->'legs' end,
           now()
    from jsonb_array_elements(rows) x
    on conflict (id) do update set
      label = excluded.label, grp = excluded.grp,
      dec = case when markets.result is null then excluded.dec else markets.dec end,
      status = excluded.status,
      result = coalesce(excluded.result, markets.result),
      max_stake = excluded.max_stake, was_dec = excluded.was_dec,
      legs = coalesce(markets.legs, excluded.legs),
      updated_at = now()
    returning 1)
  select count(*)::int from r;
$$;

-- Las selecciones de cada supermomio se fijan la primera vez que se publican.
create or replace function boost_legs(p_week int) returns json
language sql security definer set search_path = public as $$
  select coalesce(json_object_agg(id, legs), '{}'::json) from markets
  where week = p_week and legs is not null;
$$;
revoke all on function boost_legs(int) from public, anon, authenticated;

create or replace function place_bet(p_team int, p_pin text, p_markets text[], p_stake numeric, p_expected numeric)
returns json language plpgsql security definer set search_path = public, extensions as $$
declare b bettors; total numeric := 1; n int; bad text; new_id bigint; nboost int; cap numeric; used numeric;
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
  select count(*), min(max_stake) into nboost, cap from markets where id = any(p_markets) and max_stake is not null;
  if nboost > 0 then
    if n > 1 then raise exception 'Los supermomios se apuestan solos, no dentro de un parlay'; end if;
    select coalesce(sum(bt.stake), 0) into used from bets bt join bet_legs l on l.bet_id = bt.id
      where bt.team_id = p_team and l.market_id = p_markets[1];
    if used + p_stake > cap then
      raise exception 'El tope de este supermomio es $% por persona (te quedan $%)', cap, greatest(cap - used, 0);
    end if;
  end if;
  select exp(sum(ln(dec))) into total from markets where id = any(p_markets);
  if n >= 6 then total := 1 + (total - 1) * 1.15;
  elsif n >= 4 then total := 1 + (total - 1) * 1.10; end if;
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

-- El pago usa el momio del boleto (ya con boost de parlay), no el producto de las selecciones.
create or replace function settle_bets() returns int
language plpgsql security definer set search_path = public as $$
declare r record; n int := 0; pay numeric;
begin
  for r in
    select b.id, b.team_id, b.stake, b.dec,
      bool_or(m.result = 'loss') any_loss,
      bool_and(m.result is not null) all_done,
      bool_and(m.result = 'push') all_push,
      bool_or(m.result = 'push') any_push,
      exp(sum(ln(case when m.result = 'win' then l.dec else 1 end))) mult
    from bets b join bet_legs l on l.bet_id = b.id join markets m on m.id = l.market_id
    where b.status = 'open' group by b.id
  loop
    if r.any_loss then
      update bets set status = 'lost', payout = 0, settled_at = now() where id = r.id;
      n := n + 1;
    elsif r.all_done then
      -- sin selecciones anuladas se paga el momio del boleto; con anuladas, solo las que ganaron
      pay := round(r.stake * case when r.any_push then r.mult else r.dec end, 2);
      update bets set status = case when r.all_push then 'push' else 'won' end,
        payout = pay, settled_at = now() where id = r.id;
      update bettors set balance = balance + pay where team_id = r.team_id;
      n := n + 1;
    end if;
  end loop;
  return n;
end $$;
revoke all on function settle_bets() from public, anon, authenticated;
