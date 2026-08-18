REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE jhin_reader LOGIN PASSWORD 'reader-pass'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE jhin_writer LOGIN PASSWORD 'writer-pass'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

GRANT SET ON PARAMETER temp_file_limit TO jhin_reader, jhin_writer;

CREATE TABLE public.widget_groups (
  id integer PRIMARY KEY,
  label text NOT NULL
);
ALTER TABLE public.widget_groups ALTER COLUMN label SET STORAGE EXTERNAL;

CREATE TABLE public.widgets (
  id integer PRIMARY KEY,
  group_id integer NOT NULL REFERENCES public.widget_groups(id)
    ON UPDATE NO ACTION ON DELETE NO ACTION NOT DEFERRABLE,
  name text NOT NULL
);
ALTER TABLE public.widgets ALTER COLUMN name SET STORAGE EXTERNAL;
CREATE INDEX widgets_group_id_idx ON public.widgets (group_id);

INSERT INTO public.widget_groups VALUES (1, 'primary');
INSERT INTO public.widgets VALUES
  (1, 1, 'alpha'),
  (2, 1, 'beta'),
  (3, 1, repeat('x', 20000));

CREATE SCHEMA private;
REVOKE ALL ON SCHEMA private FROM PUBLIC;
CREATE TABLE private.side_effects (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source text NOT NULL
);

GRANT CONNECT ON DATABASE supabase_fixture TO jhin_reader, jhin_writer;
GRANT USAGE ON SCHEMA public TO jhin_reader, jhin_writer;
GRANT SELECT ON public.widgets, public.widget_groups TO jhin_reader;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.widgets TO jhin_writer;
GRANT SELECT ON public.widget_groups TO jhin_writer;
GRANT MAINTAIN ON public.widget_groups TO jhin_writer;

-- Keep the health sentinel last so a healthy container proves all fixture
-- roles, tables, data, and grants above were initialized successfully.
CREATE TABLE public.fixture_ready (ready boolean PRIMARY KEY);
INSERT INTO public.fixture_ready VALUES (true);
