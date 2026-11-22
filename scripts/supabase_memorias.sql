-- Robin: memória na nuvem. Rode UMA VEZ no SQL Editor de um projeto novo do Supabase, ANTES de
-- conectar o Robin em Configurações → Memória na nuvem.
-- Ordem: este arquivo primeiro (memórias), depois scripts/supabase_conversas.sql (conversas).
--
-- Cria a extensão vector, a tabela public.memories, a busca por significado (RPC match_memories)
-- e a busca por texto (coluna fts, usada pelo filtro fts=wfts(portuguese) quando não há embedding).
-- Pode rodar de novo sem estragar nada: só create ... if not exists / create or replace; nada é
-- apagado. Num projeto que já tem uma match_memories com outra assinatura, o Postgres recusa o
-- create or replace e nada muda (este script é para projeto novo).
--
-- Versão de cada linha: o updated_at é gravado pelo próprio Robin (microssegundos, UTC) e serve de
-- trava contra versão velha (update/delete com updated_at=eq.<versão>). Por isso esta tabela NÃO tem
-- o trigger robin_touch_updated_at das conversas: o servidor não pode trocar a versão sozinho.

set search_path = public, extensions;

create extension if not exists vector with schema extensions;

create table if not exists public.memories (
  id uuid primary key,
  content text not null,
  tags text[] not null default '{}',
  status text check (status in ('pending', 'done')),  -- null = guardado; pending = a fazer; done = feito
  pinned boolean not null default false,               -- true = núcleo ("Sobre a pessoa")
  embedding vector(768),                               -- gemini-embedding-001 com 768 dimensões
  request_id text not null,                            -- idempotência: reenviar não duplica
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  done_at timestamptz,
  updated_by text
);

-- Texto da busca: conteúdo + etiquetas. array_to_string não é immutable, e coluna gerada exige uma
-- expressão immutable; com text[] o resultado é sempre o mesmo, então o invólucro é seguro.
create or replace function public.robin_memory_text(content text, tags text[]) returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
  select coalesce(content, '') || ' ' || coalesce(pg_catalog.array_to_string(tags, ' '), '');
$$;

alter table public.memories
  add column if not exists fts tsvector
  generated always as (to_tsvector('portuguese'::regconfig, public.robin_memory_text(content, tags))) stored;

-- O insert usa on_conflict=request_id (ignore-duplicates): precisa de índice único nessa coluna.
create unique index if not exists memories_request_id_key on public.memories (request_id);
create index if not exists memories_fts_idx on public.memories using gin (fts);
create index if not exists memories_embedding_idx on public.memories using hnsw (embedding vector_cosine_ops);

-- Busca por significado (SupabaseStore.search e SupabaseClient.check). Similaridade de cosseno;
-- só linhas com embedding; o que já foi feito só entra com include_done.
create or replace function public.match_memories(
  query_embedding vector(768),
  match_count integer default 8,
  include_done boolean default false
)
returns table (
  id uuid,
  content text,
  tags text[],
  status text,
  pinned boolean,
  created_at timestamptz,
  done_at timestamptz,
  similarity double precision
)
language sql
stable
set search_path = public, extensions
as $$
  select m.id, m.content, m.tags, m.status, m.pinned, m.created_at, m.done_at,
         1 - (m.embedding <=> query_embedding) as similarity
  from public.memories m
  where m.embedding is not null
    and (include_done or m.status is distinct from 'done')
  order by m.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

-- Sem políticas: só a chave secreta (sb_secret_..., a que o Robin usa) lê e grava; a chave pública não.
alter table public.memories enable row level security;

-- A Data API exige permissões explícitas em projetos novos. A chave secreta usa service_role.
grant select, insert, update, delete on table public.memories to service_role;
grant execute on function public.robin_memory_text(text, text[]) to service_role;
revoke execute on function public.match_memories(vector, integer, boolean) from public, anon, authenticated;
grant execute on function public.match_memories(vector, integer, boolean) to service_role;
