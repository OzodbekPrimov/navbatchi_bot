# Navbatchi bot — ovqat navbati MVP

Xonadoshlar uchun Telegram bot: ovqat navbati, uchta kunlik eslatma, kun oxiridagi tasdiqlash va vaqtinchalik transfer.

## Arxitektura

```text
Telegram
   │
Aiogram 3 bot
   ├── Handlerlar: admin, navbat, transfer, ovoz berish
   ├── Scheduler: har 30 soniyada due vazifalarni tekshiradi
   └── PostgreSQL: navbat, kunlik vazifa, so‘rov, ovoz va xabar loglari
```

Bot bitta process bo‘lib ishlaydi. Scheduler xabar yuborilganini `notification_logs` jadvaliga yozadi; shu sababli restart yoki bir tugmani qayta bosish navbatni takror siljitmaydi. Ushbu MVP’da faqat **bitta xona va ovqat navbati** bor.

## Biznes qoidalari

- Admin ovqat navbati ketma-ketligini boshqaradi.
- Admin navbatni boshlaganda bugungi birinchi navbatchini tanlaydi.
- Navbatchiga Toshkent vaqti bilan 07:00, 12:00 va 19:00 da xabar yuboriladi. Bot vaqtincha to‘xtagan bo‘lsa, qaytganda faqat eng so‘nggi o‘tkazib yuborilgan eslatmani yuboradi.
- Navbatchi `Ovqat tayyorladim` tugmasi bilan bajarganini qayd qilishi mumkin; bu yakuniy ovoz o‘rnini bosmaydi.
- Bot guruhga 07:00 dan keyin bir marta bugungi navbatchini Telegram mention bilan e’lon qiladi. Navbatchi o‘zgarsa, guruhga yangi mention bilan yangilanish yuboriladi.
- 22:00 da navbatchidan tashqari faol qatnashchilarga tekshiruv yuboriladi.
- Ovoz berish 23:59 da yopiladi:
  - kamida 2 ta `Yo‘q` — ertaga ham o‘sha rejalashtirilgan navbatchi;
  - 2 tadan kam `Yo‘q` va kamida 1 ta `Ha` — ertaga keyingi odam;
  - ovoz bo‘lmasa, lekin navbatchi tayyorlaganini tasdiqlagan bo‘lsa — ertaga keyingi odam;
  - ovoz ham, navbatchining tasdig‘i ham bo‘lmasa — ertaga yana o‘sha odam va admin ogohlantiriladi.
- Har odam bitta ovozga ega; tugmani qayta bosib o‘z ovozini o‘zgartirishi mumkin.
- Transfer faqat bugungi vazifani o‘zgartiradi. Doimiy navbat tartibi o‘zgarmaydi.

## Ishga tushirish

1. `.env.example` faylidan `.env` yarating.
2. `BOT_TOKEN` ga BotFather bergan tokenni yozing.
3. `ADMIN_IDS` ga birinchi adminning Telegram ID sini yozing. Bir nechta admin kerak bo‘lsa vergul bilan ajrating.
4. Ishga tushiring:

```bash
docker compose up --build -d
docker compose logs -f bot
```

Telegram ID ni [@userinfobot](https://t.me/userinfobot) kabi bot orqali bilib olish mumkin. Qatnashchilar avval botga `/start` yuborishlari kerak; keyin admin ularni `⚙️ Admin → 👥 Qatnashchilar` orqali navbatga qo‘shadi.

### Guruhni ulash

1. Botni xonadoshlar Telegram guruhiga qo‘shing; unga xabar yuborish huquqi bo‘lishi kerak.
2. Guruhdagi admin `/setup_group` buyrug‘ini yuboradi.
3. Bot guruhni avtomatik saqlaydi va kunlik e’lonlarni shu yerga yuboradi.
4. Guruhni uzish kerak bo‘lsa, o‘sha guruhda `/unlink_group` yuboring.

Guruh e’lonlari va o‘zgarishlar bazada alohida loglanadi. Shu sabab server restarti ularni takror yubormaydi; Telegram xatosida bot qayta urinadi va guruhga yozish huquqi yo‘qolsa admin’ga shaxsiy ogohlantirish yuboradi.

## Birinchi sozlash

1. Admin `/start` yuboradi.
2. Har bir xonadosh botga `/start` yuboradi.
3. Admin `👥 Qatnashchilar` menyusidan kerakli odamlarni qo‘shadi.
4. Admin `↕️ Navbat tartibi` orqali ketma-ketlikni tuzadi.
5. Admin `▶️ Navbatni boshlash` orqali bugungi odamni tanlaydi.

## Loyihadagi muhim qismlar

- `app/models.py` — PostgreSQL sxemasi.
- `app/services.py` — transaction ichidagi biznes qoidalari.
- `app/scheduler.py` — eslatma, tekshiruv va tiklanish oqimi.
- `app/handlers.py` — Aiogram interfeysi.

Schema MVP uchun dastur ishga tushganda avtomatik yaratiladi. Ishlayotgan production bazasining strukturasini keyinchalik o‘zgartirishdan oldin Alembic migratsiyalari qo‘shilishi kerak.
