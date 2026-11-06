-- Generates web/src/lib/fixtures/deitythemenace.real.json from the dev database.
--
-- READ ONLY: the transaction is opened read-only, so this cannot write even by
-- mistake. Only the ingested-game sections are computed here; move-level
-- numbers do not exist until M2 runs and are synthetic (fixtures/synthetic.ts).
--
-- The definitions follow docs/api-contract.md, so this doubles as a reference
-- for M4's report SQL for these sections.
--
--   docker exec -i chess-coach-postgres-1 psql -U chess -d chess_coach -qAt \
--     -v ON_ERROR_STOP=1 < web/scripts/fixtures.sql \
--     > web/src/lib/fixtures/deitythemenace.real.json

BEGIN TRANSACTION READ ONLY;

WITH g AS (
  SELECT
    g.time_class, g.result, g.rated, g.color, g.played_at, g.player_rating,
    CASE
      WHEN g.opening_name IS NULL OR g.opening_name = 'Undefined' THEN NULL
      ELSE COALESCE(
        substring(g.opening_name FROM
          '^(.*?(Opening|Defense|Defence|Game|Gambit|System|Attack))( |$)'),
        g.opening_name)
    END AS opening,
    -- player_rating is the rating after the game, so a game's change is its
    -- rating minus the previous rated game's in the same class.
    CASE WHEN g.rated THEN
      g.player_rating - lag(g.player_rating) OVER (
        PARTITION BY g.time_class, g.rated ORDER BY g.played_at, g.id)
    END AS rating_change
  FROM games g
  JOIN players p ON p.id = g.player_id
  WHERE p.username = 'deitythemenace' AND g.rules = 'chess'
),
progress AS (
  SELECT g.time_class,
         count(*) AS games,
         count(*) FILTER (WHERE g.analysis_status = 'skipped') AS skipped
  FROM games g JOIN players p ON p.id = g.player_id
  WHERE p.username = 'deitythemenace'
  GROUP BY g.time_class
),
summary AS (
  SELECT time_class,
         count(*) AS games,
         count(*) FILTER (WHERE result = 'win') AS wins,
         count(*) FILTER (WHERE result = 'draw') AS draws,
         count(*) FILTER (WHERE result = 'loss') AS losses,
         100.0 * count(*) FILTER (WHERE result = 'win') / count(*) AS win_rate_pct,
         (array_agg(player_rating ORDER BY played_at DESC)
            FILTER (WHERE rated AND player_rating IS NOT NULL))[1] AS current_rating
  FROM g GROUP BY time_class
),
days AS (
  SELECT time_class, (played_at AT TIME ZONE 'UTC')::date AS day,
         (array_agg(player_rating ORDER BY played_at DESC))[1] AS rating,
         count(*) AS games
  FROM g WHERE rated AND player_rating IS NOT NULL
  GROUP BY 1, 2
),
trend AS (
  SELECT time_class,
         json_agg(json_build_object('date', to_char(day, 'YYYY-MM-DD'),
                                    'rating', rating, 'games', games) ORDER BY day) AS points
  FROM days GROUP BY time_class
),
openings_grouped AS (
  SELECT time_class, opening, color,
         count(*) AS games,
         count(*) FILTER (WHERE result = 'win') AS wins,
         count(*) FILTER (WHERE result = 'draw') AS draws,
         count(*) FILTER (WHERE result = 'loss') AS losses,
         100.0 * count(*) FILTER (WHERE result = 'win') / count(*) AS win_rate_pct,
         COALESCE(sum(rating_change), 0)::int AS rating_change
  FROM g WHERE opening IS NOT NULL
  GROUP BY 1, 2, 3 HAVING count(*) >= 5
),
openings AS (
  SELECT time_class,
         json_agg(json_build_object(
           'opening', opening, 'color', color, 'games', games, 'wins', wins,
           'draws', draws, 'losses', losses, 'win_rate_pct', win_rate_pct,
           'rating_change', rating_change)
           ORDER BY rating_change, games DESC, opening, color) AS rows
  FROM openings_grouped GROUP BY time_class
)
SELECT json_build_object(
  'source', 'chess_coach dev database, player deitythemenace, read-only',
  'generated_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'time_classes', json_object_agg(pr.time_class, json_build_object(
    'ingested', pr.games,
    'skipped', pr.skipped,
    'games', COALESCE(s.games, 0),
    'wins', COALESCE(s.wins, 0),
    'draws', COALESCE(s.draws, 0),
    'losses', COALESCE(s.losses, 0),
    'win_rate_pct', s.win_rate_pct,
    'current_rating', s.current_rating,
    'rating_trend', COALESCE(t.points, '[]'::json),
    'openings', COALESCE(o.rows, '[]'::json)
  ))
)
FROM progress pr
LEFT JOIN summary s USING (time_class)
LEFT JOIN trend t USING (time_class)
LEFT JOIN openings o USING (time_class);

ROLLBACK;
