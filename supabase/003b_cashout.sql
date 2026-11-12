-- Parte 2: cerrar apuesta (cash out)
create or replace function sync_markets(rows jsonb) returns int
language sql security definer set search_path = public as $$
  with r as (
    insert into markets(id, week, grp, label, dec, open_dec, status, result, max_stake, was_dec, legs, prob, updated_at)
    select x->>'id', (x->>'week')::int, x->>'grp', x->>'label', (x->>'dec')::numeric, (x->>'dec')::numeric,
           x->>'status', nullif(x->>'result', ''), (x->>'max_stake')::numeric, (x->>'was_dec')::numeric,
           case when jsonb_typeof(x->'legs') = 'array' then x->'legs' end,
           (x->>'prob')::numeric, now()
    from jsonb_array_elements(rows) x
    on conflict (id) do update set
      label = excluded.label, grp = excluded.grp,
      dec = case when markets.result is null then excluded.dec else markets.dec end,
      status = excluded.status,
      result = coalesce(excluded.result, markets.result),
      max_stake = excluded.max_stake, was_dec = excluded.was_dec,
      legs = coalesce(markets.legs, excluded.legs),
      prob = excluded.prob,
      updated_at = now()
    returning 1)
  select count(*)::int from r;
$$;

-- Valor para cerrar un boleto: pago posible x probabilidad actual de lo que falta, menos 10%.
-- No se ofrece si alguna selección pendiente está en juego o ya perdió.
create or replace function _cashout_value(p_bet bigint) returns numeric
language plpgsql security definer set search_path = public as $$
declare r record; pay numeric; v numeric;
begin
  select b.stake, b.dec, b.status,
    bool_or(m.result = 'loss') any_loss,
    bool_or(m.result is null and (m.status <> 'open' or m.prob is null)) blocked,
    bool_and(m.result is not null) all_done,
    bool_or(m.result = 'push') any_push,
    exp(sum(ln(case when m.result is null then greatest(m.prob, 0.0001) else 1 end))) p_rest,
    exp(sum(ln(case when m.result = 'push' then 1 else l.dec end))) mult
  into r
  from bets b join bet_legs l on l.bet_id = b.id join markets m on m.id = l.market_id
  where b.id = p_bet group by b.id;
  if r is null or r.status <> 'open' or r.any_loss or r.blocked or r.all_done then return null; end if;
  pay := r.stake * case when r.any_push then r.mult else r.dec end;
  v := round(pay * r.p_rest * 0.90, 2);
  if v < 1 then return null; end if;
  return v;
end $$;
revoke all on function _cashout_value(bigint) from public, anon, authenticated;

create or replace function cash_out(p_team int, p_pin text, p_bet bigint, p_expected numeric) returns json
language plpgsql security definer set search_path = public, extensions as $$
declare b bettors; v numeric;
begin
  b := _auth(p_team, p_pin);
  if not exists (select 1 from bets where id = p_bet and team_id = p_team and status = 'open') then
    raise exception 'Este boleto ya no está abierto';
  end if;
  v := _cashout_value(p_bet);
  if v is null then raise exception 'Ahorita no se puede cerrar este boleto (hay partidos en juego)'; end if;
  if abs(v - p_expected) > greatest(1, p_expected * 0.01) then
    raise exception 'El valor para cerrar cambió: ahora es $%', v;
  end if;
  update bets set status = 'cashed', payout = v, settled_at = now() where id = p_bet;
  update bettors set balance = balance + v where team_id = p_team;
  return json_build_object('payout', v, 'balance', b.balance + v);
end $$;
grant execute on function cash_out(int, text, bigint, numeric) to anon, authenticated;

create or replace function my_bets(p_team int, p_pin text) returns json
language plpgsql security definer set search_path = public, extensions as $$
declare b bettors;
begin
  b := _auth(p_team, p_pin);
  return json_build_object('balance', b.balance, 'bets', coalesce((
    select json_agg(x order by x.created_at desc) from (
      select bt.id, bt.stake, bt.dec, bt.status, bt.payout, bt.created_at,
        case when bt.status = 'open' then _cashout_value(bt.id) end cashout,
        (select json_agg(json_build_object('label', l.label, 'dec', l.dec, 'result', m.result))
           from bet_legs l join markets m on m.id = l.market_id where l.bet_id = bt.id) legs
      from bets bt where bt.team_id = p_team limit 200) x), '[]'::json));
end $$;
