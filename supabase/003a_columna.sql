-- Parte 1: probabilidad actual de cada mercado (para cerrar apuestas)
alter table markets add column prob numeric;
