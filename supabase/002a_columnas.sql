-- Parte 1: columnas nuevas para promociones
alter table markets add column max_stake numeric;
alter table markets add column was_dec numeric;
alter table markets add column legs jsonb;
