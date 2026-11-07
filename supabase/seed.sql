-- Synthetic demo data only. Never load real patient data.
insert into public.clinic (slug, name, emergency_number, response_window, timezone)
values ('sunrise', 'Sunrise Fertility Clinic', '999', '12 to 18 hours', 'Asia/Kuala_Lumpur')
on conflict (slug) do nothing;
