-- Robin: conversas na nuvem (fase 3). Rode uma vez no SQL Editor do seu projeto do Supabase.
-- Não mexe na tabela memories nem na função match_memories. Pode rodar de novo sem estragar nada.
--
-- Uma linha por conversa e uma por mensagem (o app só insere mensagens: reenviar não duplica).
-- Apagar uma conversa deixa só a marca (deleted, sem o título) e apaga as mensagens dela, para os
-- outros PCs apagarem também. O updated_at é sempre do servidor (trigger), e o app puxa só o que
-- mudou desde o último pull, sem depender do relógio de cada PC.

create table if not exists public.conversations (
  id uuid primary key,
  title text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  updated_by text,
  deleted boolean not null default false
);

create table if not exists public.conversation_messages (
  id uuid primary key,
  conversation_id uuid not null references public.conversations (id) on delete cascade,
  role text not null check (role in ('user', 'robin')),
  text text not null default '',
  actions jsonb,
  status text check (status in ('stopped', 'error')),
  error jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted boolean not null default false
);

create index if not exists conversations_updated_at_idx on public.conversations (updated_at);
create index if not exists conversation_messages_updated_at_idx on public.conversation_messages (updated_at);
create index if not exists conversation_messages_conversation_idx on public.conversation_messages (conversation_id, created_at);

-- updated_at do servidor em toda inserção e alteração.
create or replace function public.robin_touch_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists conversations_touch on public.conversations;
create trigger conversations_touch before insert or update on public.conversations
  for each row execute function public.robin_touch_updated_at();

drop trigger if exists conversation_messages_touch on public.conversation_messages;
create trigger conversation_messages_touch before insert or update on public.conversation_messages
  for each row execute function public.robin_touch_updated_at();

-- Sem políticas: só a chave secreta (sb_secret_..., a que o Robin usa) lê e grava; a chave pública não.
alter table public.conversations enable row level security;
alter table public.conversation_messages enable row level security;

-- A Data API exige permissões explícitas em projetos novos. A chave secreta usa service_role.
grant select, insert, update on table public.conversations to service_role;
grant select, insert, update, delete on table public.conversation_messages to service_role;
