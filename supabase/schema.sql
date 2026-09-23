create extension if not exists pgcrypto;

create table if not exists public.try_on_gallery (
  id uuid primary key default gen_random_uuid(),
  anonymous_user_id uuid not null,
  category text not null,
  product_source text not null check (product_source in ('upload', 'scraped_url', 'image_url', 'wardrobe')),
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

-- Upgrades for projects created before outfit try-ons. Safe to run more than once.
alter table public.try_on_gallery add column if not exists items jsonb not null default '[]'::jsonb;
alter table public.try_on_gallery drop constraint if exists try_on_gallery_product_source_check;
alter table public.try_on_gallery add constraint try_on_gallery_product_source_check
  check (product_source in ('upload', 'scraped_url', 'image_url', 'wardrobe'));

-- Online wardrobe: 'store' items are products saved from shops, 'home' items are clothes the user already owns.
create table if not exists public.wardrobe_items (
  id uuid primary key default gen_random_uuid(),
  anonymous_user_id uuid not null,
  collection text not null check (collection in ('store', 'home')),
  slot text not null check (slot in ('top', 'bottom', 'dress', 'outerwear', 'footwear', 'jewelry', 'accessory', 'other')),
  name text not null,
  brand text,
  color text,
  price numeric(12, 2),
  currency text,
  sizes jsonb not null default '[]'::jsonb,
  selected_size text,
  store text,
  product_url text,
  source_image_url text,
  notes text,
  image_path text not null,
  created_at timestamptz not null default now()
);

create index if not exists wardrobe_items_user_created_idx
  on public.wardrobe_items (anonymous_user_id, created_at desc);

alter table public.wardrobe_items enable row level security;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('fitcart-tryons', 'fitcart-tryons', false, 20000000, array['image/jpeg','image/png','image/webp'])
on conflict (id) do update set public = false;
