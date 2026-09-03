-- AVE 서버 분석 데이터 스키마
-- Supabase SQL Editor에서 실행한다.

create extension if not exists pgcrypto;

do $$ begin
  create type public.analysis_job_status as enum (
    'queued', 'collecting', 'analyzing', 'rendering', 'completed', 'failed', 'cancelled'
  );
exception when duplicate_object then null;
end $$;

alter type public.analysis_job_status add value if not exists 'cancelled';

create table if not exists public.analysis_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  client_job_id text not null,
  source_id text not null,
  source_url text,
  title text,
  duration_ms bigint check (duration_ms is null or duration_ms >= 0),
  status public.analysis_job_status not null default 'queued',
  progress smallint not null default 0 check (progress between 0 and 100),
  error_message text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (user_id, client_job_id)
);

create table if not exists public.analysis_results (
  job_id uuid primary key references public.analysis_jobs(id) on delete cascade,
  script text,
  segments jsonb not null default '[]'::jsonb,
  heatmap jsonb not null default '[]'::jsonb,
  recommendation jsonb not null default '{}'::jsonb,
  selection jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists public.transcription_jobs (
  runpod_job_id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  client_job_id text not null,
  server_job_id uuid references public.analysis_jobs(id) on delete set null,
  file_id text not null,
  phase text not null default 'transcription',
  status text not null check (status in ('queued', 'in_progress', 'cancel_requested', 'cancelled', 'completed', 'failed')),
  progress smallint not null default 0 check (progress between 0 and 100),
  message text,
  last_heartbeat_at timestamptz not null default now(),
  lease_expires_at timestamptz not null,
  cancel_requested_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, client_job_id, runpod_job_id)
);

create index if not exists analysis_jobs_user_created_idx
  on public.analysis_jobs(user_id, created_at desc);

create index if not exists transcription_jobs_lease_idx
  on public.transcription_jobs(status, lease_expires_at);

alter table public.analysis_jobs enable row level security;
alter table public.analysis_results enable row level security;
alter table public.transcription_jobs enable row level security;

drop policy if exists "analysis jobs: own rows" on public.analysis_jobs;
create policy "analysis jobs: own rows" on public.analysis_jobs
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "analysis results: owner through job" on public.analysis_results;
create policy "analysis results: owner through job" on public.analysis_results
  for all using (
    exists (
      select 1 from public.analysis_jobs
      where analysis_jobs.id = job_id and analysis_jobs.user_id = auth.uid()
    )
  ) with check (
    exists (
      select 1 from public.analysis_jobs
      where analysis_jobs.id = job_id and analysis_jobs.user_id = auth.uid()
    )
  );

drop policy if exists "transcription jobs: own rows" on public.transcription_jobs;
create policy "transcription jobs: own rows" on public.transcription_jobs
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
