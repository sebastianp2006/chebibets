# ChebiBets

Sportsbook de juego para la liga "Fantasy Oficial" de ESPN.

- `scripts/update.py` junta proyecciones de ESPN, RotoWire (vía Sleeper) y CBS, calcula momios de matchups, props y futuros, y escribe `site/data.json`.
- `site/index.html` es la página.
- `supabase/schema.sql` guarda saldos y boletos con dinero de juego.
- `.github/workflows/update.yml` lo corre diario y cada 10 minutos en días de partido.
