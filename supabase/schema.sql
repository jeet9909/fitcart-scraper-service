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

-- Look allowances. Every signed-in account gets a free grant each month (India time); passes and
-- subscriptions bought through Razorpay add more. A try-on spends one look from the grant that expires first.
create table if not exists public.look_grants (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  kind text not null check (kind in ('free', 'pass', 'plus', 'pro', 'bonus')),
  looks integer not null check (looks >= 0),
  used integer not null default 0 check (used >= 0 and used <= looks),
  period text,
  starts_at timestamptz not null default now(),
  expires_at timestamptz not null,
  payment_ref text unique,
  created_at timestamptz not null default now()
);

create unique index if not exists look_grants_free_month_idx
  on public.look_grants (user_id, period) where kind = 'free';
create index if not exists look_grants_user_expiry_idx
  on public.look_grants (user_id, expires_at);

-- Projects that ran an earlier version of this file named the payment column stripe_ref.
do $$
begin
  if exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'look_grants' and column_name = 'stripe_ref') then
    alter table public.look_grants rename column stripe_ref to payment_ref;
  end if;
end $$;

alter table public.look_grants enable row level security;

create or replace function public.ensure_free_looks(p_user uuid, p_looks integer)
returns void language sql security definer set search_path = public as $$
  insert into public.look_grants (user_id, kind, looks, period, starts_at, expires_at)
  select p_user, 'free', p_looks, to_char(m, 'YYYY-MM'), m at time zone 'Asia/Kolkata', (m + interval '1 month') at time zone 'Asia/Kolkata'
  from (select date_trunc('month', now() at time zone 'Asia/Kolkata') as m) as month
  where p_looks > 0
  on conflict do nothing;
$$;

-- Spends one look and returns the grant it came from, or null when none are left.
create or replace function public.consume_look(p_user uuid, p_free_looks integer)
returns uuid language plpgsql security definer set search_path = public as $$
declare
  v_id uuid;
begin
  perform public.ensure_free_looks(p_user, p_free_looks);
  for attempt in 1..3 loop
    update public.look_grants set used = used + 1
    where id = (
      select id from public.look_grants
      where user_id = p_user and used < looks and starts_at <= now() and expires_at > now()
      order by expires_at, created_at
      limit 1
      for update
    ) and used < looks
    returning id into v_id;
    if v_id is not null then
      return v_id;
    end if;
  end loop;
  return null;
end;
$$;

-- Gives a look back when a try-on fails.
create or replace function public.refund_look(p_grant uuid)
returns void language sql security definer set search_path = public as $$
  update public.look_grants set used = used - 1 where id = p_grant and used > 0;
$$;

-- Only the API (service role) may touch allowances; the public anon key must not.
revoke all on function public.ensure_free_looks(uuid, integer) from public, anon, authenticated;
revoke all on function public.consume_look(uuid, integer) from public, anon, authenticated;
revoke all on function public.refund_look(uuid) from public, anon, authenticated;
grant execute on function public.ensure_free_looks(uuid, integer) to service_role;
grant execute on function public.consume_look(uuid, integer) to service_role;
grant execute on function public.refund_look(uuid) to service_role;
