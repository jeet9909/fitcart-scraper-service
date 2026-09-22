create extension if not exists pgcrypto;

create table if not exists public.try_on_gallery (
  id uuid primary key default gen_random_uuid(),
  anonymous_user_id uuid not null,
  category text not null,
  product_source text not null check (product_source in ('upload', 'scraped_url', 'image_url')),
  product_url text,
  person_path text not null,
  product_path text not null,
  result_path text not null,
  model text not null,
  created_at timestamptz not null default now()
);

create index if not exists try_on_gallery_user_created_idx
  on public.try_on_gallery (anonymous_user_id, created_at desc);

alter table public.try_on_gallery enable row level security;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('fitcart-tryons', 'fitcart-tryons', false, 20000000, array['image/jpeg','image/png','image/webp'])
on conflict (id) do update set public = false;
