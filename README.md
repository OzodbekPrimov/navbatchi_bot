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

Bot bitta process bo‘lib ishlaydi. Scheduler xabar yuborilganini bazada qayd qiladi; shu sababli restart yoki bir tugmani qayta bosish navbatni takror siljitmaydi. Ushbu MVP bitta xona uchun ovqat, non va suv navbatlarini boshqaradi.

## Biznes qoidalari

- Admin ovqat navbati ketma-ketligini boshqaradi.
- Admin navbatni boshlaganda bugungi birinchi navbatchini tanlaydi.
- Navbatchiga Toshkent vaqti bilan 07:00, 12:00 va 19:00 da xabar yuboriladi. Bot vaqtincha to‘xtagan bo‘lsa, qaytganda faqat eng so‘nggi o‘tkazib yuborilgan eslatmani yuboradi.
- Navbatchi `Ovqat tayyorladim` tugmasi bilan bajarganini qayd qilishi mumkin; bu yakuniy ovoz o‘rnini bosmaydi.
- Bot guruhga 07:00 dan keyin bir marta bugungi navbatchini Telegram mention bilan e’lon qiladi. Navbatchi o‘zgarsa, guruhga yangi mention bilan yangilanish yuboriladi.
- 22:00 da navbatchidan tashqari faol qatnashchilarga tekshiruv yuboriladi.
- Ovoz berish 23:59 da yopiladi:
  - kamida 1 ta `Yo‘q` — ertaga ham o‘sha rejalashtirilgan navbatchi;
  - `Yo‘q` bo‘lmasa va kamida 1 ta `Ha` bo‘lsa — ertaga keyingi odam;
  - ovoz bo‘lmasa, lekin navbatchi tayyorlaganini tasdiqlagan bo‘lsa — ertaga keyingi odam;
  - ovoz ham, navbatchining tasdig‘i ham bo‘lmasa — ertaga yana o‘sha odam va admin ogohlantiriladi.
- Har odam bitta ovozga ega; tugmani qayta bosib o‘z ovozini o‘zgartirishi mumkin.
- Transfer faqat bugungi vazifani o‘zgartiradi. Doimiy navbat tartibi o‘zgarmaydi.

### Non va suv navbati

- Non va suv alohida, kunlik bo‘lmagan navbatlardir.
- Admin har biri uchun alohida qatnashchilar va boshlang‘ich/keyingi odamni belgilaydi.
- Xonadosh `🥖 Non tugadi` yoki `💧 Suv tugadi` ni tasdiqlasa, bot faqat bitta ochiq vazifa yaratadi.
- Navbatchi guruhda mention qilinadi va shaxsiy chatida `Olib keldim` hamda `Navbatni o‘tkazish` tugmalarini oladi.
- `Olib keldim` bosilgach, qolgan faol xonadoshlarga 30 daqiqalik `Ha / Yo‘q` tekshiruv yuboriladi.
  - kamida 1 ta `Yo‘q` — vazifa o‘sha odamda qoladi;
  - `Yo‘q` bo‘lmasa va kamida 1 ta `Ha` bo‘lsa — navbat keyingi odamga siljiydi;
  - ovoz bo‘lmasa — `Olib keldim` tasdig‘i asosida navbat siljiydi.
- Transfer faqat tekshiruv boshlanishidan oldin mumkin. Qabul qilinsa, eski va yangi navbatchi shaxsiy xabar oladi, guruhda ikkalasi mention bilan e’lon qilinadi. So‘rov 15 daqiqada qabul qilinmasa avtomatik bekor bo‘ladi.
- Admin tekshiruv boshlanmaguncha `Faol vazifani almashtirish` orqali non yoki suv vazifasini bevosita boshqa navbatchiga bera oladi.

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
2. `ADMIN_IDS` ichidagi va Telegram guruhida ham admin bo‘lgan foydalanuvchi `/setup_group` buyrug‘ini yuboradi.
3. Bot guruhni avtomatik saqlaydi va kunlik e’lonlarni shu yerga yuboradi.
4. Guruhni uzish kerak bo‘lsa, o‘sha guruhda `/unlink_group` yuboring.

Guruh e’lonlari va o‘zgarishlar bazada alohida loglanadi. Shu sabab server restarti ularni takror yubormaydi; Telegram xatosida bot qayta urinadi va guruhga yozish huquqi yo‘qolsa admin’ga shaxsiy ogohlantirish yuboradi.

## Birinchi sozlash

1. Admin `/start` yuboradi.
2. Har bir xonadosh botga `/start` yuboradi.
3. Admin `👥 Qatnashchilar` menyusidan kerakli odamlarni qo‘shadi.
4. Admin `↕️ Navbat tartibi` orqali ketma-ketlikni tuzadi.
5. Admin `▶️ Navbatni boshlash` orqali bugungi odamni tanlaydi.
6. Admin `📦 Non va suv navbati` orqali non va suv qatnashchilarini qo‘shadi, tartibini tuzadi va har biri uchun keyingi navbatchini tanlaydi.

## Kundalik foydalanish

- `📋 Bugungi holat` — ovqatning bugungi odami, non/suvning faol vazifasi yoki keyingi odamini ko‘rsatadi.
- `📌 Mening navbatlarim` — foydalanuvchining ovqat vazifasi va non/suvdagi navbat o‘rnini ko‘rsatadi; faol vazifa bo‘lsa kerakli tugmalar ham chiqadi.
- `⚙️ Admin → 📊 Umumiy holat` — uchala navbatning bitta ekrandagi holati.

## Tarix

`📜 Tarix` menyusi ovqat, suv va non uchun yakunlangan vazifalarni alohida bo‘limlarda chiqaradi. Har bo‘limda oxirgi 10 ta yozuv va eski yozuvlarga o‘tish tugmalari bo‘ladi; transfer yuz berganda rejalashtirilgan hamda amalda bajargan xonadosh ko‘rsatiladi.

## Qo‘lda eslatma

`⚙️ Admin → 🔔 Eslatmalar` orqali admin ayni paytdagi ovqat, non yoki suv navbatchisiga, yoxud barcha faol navbatchilarga eslatma yuboradi. Har eslatma navbatchining shaxsiy chatiga va faol guruhga alohida yetkaziladi. Bir vazifa uchun qo‘lda yuborish 30 daqiqada bir marta bilan cheklanadi; Telegram xatosida xabar outbox orqali qayta yuboriladi.

## Loyihadagi muhim qismlar

- `app/models.py` — PostgreSQL sxemasi.
- `app/services.py` — transaction ichidagi biznes qoidalari.
- `app/scheduler.py` — eslatma, tekshiruv va tiklanish oqimi.
- `app/handlers.py` — Aiogram interfeysi.

Schema MVP uchun dastur ishga tushganda avtomatik yaratiladi. Ishlayotgan production bazasining strukturasini keyinchalik o‘zgartirishdan oldin Alembic migratsiyalari qo‘shilishi kerak.
