-- Parte 1: columnas nuevas para promociones (una sola instrucción)
alter table markets add column max_stake numeric, add column was_dec numeric, add column legs jsonb;
