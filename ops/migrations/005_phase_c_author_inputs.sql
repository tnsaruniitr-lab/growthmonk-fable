-- Wave C3: the convergence-fix inputs live on the site (until brandsmith profiles land).
alter table sites add column if not exists author jsonb not null default '{}';
-- {"name": ..., "title": ..., "sameAs": ["https://linkedin.com/in/..."], "credentials": ...}
alter table sites add column if not exists first_party jsonb not null default '[]';
-- [{"fact": "...", "source": "..."}] — real stats/claims the writer may cite; never invented
