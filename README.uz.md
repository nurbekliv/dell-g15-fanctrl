# dell-g15-fanctrl — Linux kuler monitoringi va qo'lda boshqaruvi

> English: [README.md](README.md)

Noutbuk/kompyuter kulerlarini kuzatish va **qo'lda** boshqarish uchun mo'ljallangan
dastur. Grafik oyna (PyQt5), buyruq qatori (`fanctrl`) va root huquqi bilan ishlaydigan
kichik demon (`fanctrld`) dan iborat.

Sinovdan o'tgan tizim: **Dell G15 5511**, Ubuntu 22.04, kernel 6.8, `dell_smm` drayveri
(2 ta kuler: `pwm1`, `pwm2`).

## Nima uchun demon kerak

`/sys/class/hwmon/*/pwm*` fayllariga faqat root yoza oladi. Butun dasturni root ostida
ishlatish o'rniga:

* `fanctrld` — root, systemd xizmati sifatida ishlaydi; sensorlarni o'qiydi, kuler tezligini yozadi
  va **xavfsizlik nazoratini** (governor) ushlab turadi;
* GUI/CLI — oddiy foydalanuvchi huquqida ishlaydi va demonga `/run/fanctrld.sock` unix
  soketi orqali murojaat qiladi (soketga faqat sozlamadagi `socket_group` guruhi kira oladi).

Bu shuni anglatadiki: GUI yopilsa ham qo'lda o'rnatilgan tezlik saqlanadi va harorat
oshib ketsa demon o'zi choralar ko'radi.

## O'rnatish

```bash
sudo apt install acpi-call-dkms python3-pyqt5
git clone https://github.com/nurbekliv/dell-g15-fanctrl.git
cd dell-g15-fanctrl
sudo ./install.sh
```

O'rnatuvchi quyidagilarni bajaradi:

| Nima | Qayerga |
|---|---|
| Python paketi | `/opt/fanctrl/fanctrl/` |
| Buyruqlar | `/usr/local/bin/fanctrl`, `/usr/local/bin/fanctrl-gui` |
| Sozlama | `/etc/fanctrld.conf` (soket guruhi = sizning guruhingiz) |
| systemd xizmati | `/etc/systemd/system/fanctrld.service` (yoqiladi va ishga tushiriladi) |
| Menyu yorlig'i | `/usr/share/applications/fanctrl.desktop` ("Fan Control") |

O'chirish: `sudo ./uninstall.sh` (sozlamani ham o'chirish uchun `--purge`).

## Ishlatish

### Grafik oyna

```bash
fanctrl-gui           # yoki dasturlar menyusidan "Fan Control"
```

* Yuqorida — **Avto (BIOS)** / **Qo'lda** tugmalari. Slayderlar faqat qo'lda rejimda faol.
* Har bir kuler uchun: joriy RPM, slayder (0–100%) va 0/25/50/75/100% tugmalari.
* O'rtada — CPU/GPU harorati va kuler tezligi grafigi (oxirgi ~3 daqiqa).
* **Kalibrlash** tugmasi faqat hwmon pwm usulida ko'rinadi (pwm pog'onalarini aniqlaydi).

### Yuqori paneldagi indikator (GNOME kengaytmasi)

Eng qulay usul — alohida oyna ochish shart emas, hamma narsa yuqori o'ng burchakda:

```bash
./install-extension.sh      # sudo KERAK EMAS
# so'ng sessiyadan chiqib qayta kiring (Wayland'da GNOME Shell ni qayta yuklab bo'lmaydi)
```

Panelda `66° · 2.6k` ko'rinishida eng issiq sensor va kuler tezligi turadi. Bosilganda
menyu ochiladi: harorat qatori, har bir kuler uchun slayder, 0/25/50/75/100% tugmalari,
**Qo'lda boshqaruv** kaliti va **Oynani ochish…** bandi.

Menyu yopiq bo'lganda demon 5 soniyada bir, ochiq bo'lganda 1 soniyada bir so'raladi.
Barcha aloqa asinxron — GNOME Shell hech qachon kutib qolmaydi.

```bash
gnome-extensions info fanctrl@nurbekliv.github.io     # holatini ko'rish
gnome-extensions disable fanctrl@nurbekliv.github.io  # vaqtincha o'chirish
journalctl -f -o cat /usr/bin/gnome-shell      # kengaytma xatolarini ko'rish
```

### Klaviatura yorlig'i

Istalgan oynadan turib grafik oynani ochish uchun:

```bash
./setup-hotkey.sh                     # Super+Shift+F
./setup-hotkey.sh '<Super>F9'         # boshqa kombinatsiya
```

### Buyruq qatori

```bash
fanctrl status            # joriy holat
fanctrl status -a         # barcha sensorlar bilan
fanctrl watch -n 1        # jonli monitoring (Ctrl+C bilan chiqish)

fanctrl manual            # qo'lda rejimga o'tish
fanctrl set all 70%       # ikkala kulerni 70% ga
fanctrl set 1 max         # 1-kuler maksimal (100%)
fanctrl set 2 0           # 2-kulerni BIOS ixtiyoriga qaytarish
fanctrl set 1 200         # pwm qiymati ham qabul qilinadi (0–255 → foizga o'giriladi)
fanctrl auto              # BIOS avtomatik boshqaruviga qaytarish

fanctrl profile                    # BIOS termal rejimlari ro'yxati
fanctrl profile UltraPerformance   # rejimni almashtirish
fanctrl calibrate         # pwm pog'onalarini aniqlash
fanctrl config            # demon sozlamalarini ko'rish

fanctrl battery                 # batareya holati va quvvatlanish rejimlari
fanctrl battery PrimAcUse       # "doim rozetkada" rejimi
fanctrl battery custom 50 60    # 50% dan pastga tushsa quvvatlansin, 60% da to'xtasin
```

`fanctrl set` avtomatik ravishda qo'lda rejimni yoqadi. Dell G15 5511 da bu
AWCC boost orqali ishlaydi (quyidagi "Boshqaruv usullari" bo'limiga qarang).

## Xavfsizlik nazorati (governor)

Nazoratning qattiqligi boshqaruv usuliga bog'liq.

**AWCC boost usulida** (Dell G15 — joriy holat) qo'lda sozlama sovutishni faqat
oshiradi, ya'ni u sabab qizib ketish mumkin emas. Shuning uchun demon bu yerda
boshqaruvni hech qachon tortib olmaydi:

| Chegara | Standart | Nima bo'ladi |
|---|---|---|
| `warn_temp` | 85 °C | Boost vaqtincha 100% ga ko'tariladi |
| `warn_temp - warn_hysteresis` | 79 °C | Sizning qiymatingiz tiklanadi |
| sensorlar o'qilmasa | — | Boost 100% (eng xavfsiz holat) |

**hwmon pwm usulida** qo'lda sozlama kulerni sekinlata oladi, shuning uchun himoya
qattiqroq:

| Chegara | Standart | Nima bo'ladi |
|---|---|---|
| `warn_temp` | 85 °C | Qo'lda sozlama vaqtincha bekor qilinadi, kuler 100% ga o'tadi |
| `warn_temp - warn_hysteresis` | 79 °C | Harorat pasaygach qo'lda sozlama tiklanadi |
| `critical_temp` | 93 °C | Qo'lda rejim butunlay bekor qilinadi, boshqaruv BIOS'ga qaytadi va `lockout_seconds` (120 s) davomida qo'lda rejim bloklanadi |
| sensorlar o'qilmasa | — | Avtomatik rejimga qaytiladi |

Ikkala usulda ham:

* yozuv ketma-ket 3 marta xato bersa — avtomatik rejimga qaytiladi;
* demon to'xtatilganda (`systemctl stop fanctrld`, tizim o'chishi) — kulerlar avtomatik
  boshqaruvga qaytariladi (boost 0).

Nazorat faqat **CPU, GPU va SMM** sensorlarini hisobga oladi (NVMe va Wi-Fi ko'rsatiladi,
lekin ular o'z himoyasiga ega).

## Boshqaruv usullari

Demon ishga tushganda qaysi usul ishlashini o'zi aniqlaydi:

| Usul | Nima qiladi | Qachon tanlanadi |
|---|---|---|
| **AWCC boost** | Kulerga 0–100% boost beradi (ACPI `\_SB.AMWW.WMAX`) | Dell/Alienware G-seriya + `acpi_call` moduli bor |
| **hwmon pwm** | Klassik `pwm1`/`pwm2` yozuvi (0–255) | pwm yozuvi haqiqatda ta'sir qilsa |
| yo'q | faqat monitoring | ikkalasi ham ishlamasa |

Dell G15 5511 da **AWCC boost** ishlatiladi. O'lchangan natija:

| Boost | Kuler 1 | Kuler 2 |
|---|---|---|
| 0% (BIOS o'zi) | 1550 RPM | 1588 RPM |
| 45% | 2507 RPM | 2476 RPM |
| 60% | 3100 RPM | 3193 RPM |
| **100%** | **4854 RPM** | **5119 RPM** |

```bash
fanctrl set all 60%     # ikkala kuler
fanctrl set 1 100%      # faqat 1-kuler (CPU tomoni)
fanctrl set 2 0         # 2-kulerni BIOS ixtiyoriga qaytarish
fanctrl auto            # ikkalasini ham BIOS ixtiyoriga
```

### Boost nima va nega u xavfsiz

Boost BIOS'ning o'z egri chizig'i **ustiga qo'shiladi** — kulerni faqat tezlashtiradi,
sekinlata olmaydi:

* `0%` = BIOS o'zi boshqaradi (odatdagi holat);
* `100%` = kuler maksimal tezlikda (hwmon ko'rsatgan "maks" dan ham yuqori).

Shuning uchun bu usulda noto'g'ri sozlama tufayli qizib ketish **mumkin emas**.
Demon buni biladi va bu usulda qo'lda rejimni hech qachon tortib olmaydi — harorat
`warn_temp` dan oshsa, faqat boost'ni vaqtincha 100% ga ko'taradi.

Talab: `acpi-call-dkms` paketi. `install.sh` uni tekshiradi, modulni yuklaydi va
`/etc/modules-load.d/acpi_call.conf` orqali har yuklanishda avtomatik yuklanishini
ta'minlaydi. Paket o'zi o'rnatilmagan bo'lsa:

```bash
sudo apt install acpi-call-dkms
sudo ./install.sh
```

Demon to'xtaganda yoki `fanctrl auto` da boost 0 ga qaytariladi, ya'ni tizim
har doim BIOS boshqaruviga qaytadi.

## Nega hwmon pwm bu noutbukda ishlamaydi

Bu tekshirilgan va o'lchangan fakt. `/sys/class/hwmon/hwmon5/pwm1` ga yozish
xatosiz o'tadi, lekin firmware qiymatni qabul qilmaydi — o'qib ko'rilganda
doim 128 bo'lib qolaveradi:

```
so'raldi=255 | darrov=128 | 0.3s=128 | 3s=128
so'raldi=0   | darrov=128 | 0.3s=128 | 3s=128
```

Sababi: `pwm1_enable` fayli yo'q — drayver BIOS'da SMM orqali qo'lda fan rejimiga
o'tish qo'llab-quvvatlanmasligini aniqlagan. Dell'ning 2021-yildan keyingi
firmware'lari eski i8k/SMM fan buyruqlarini bloklaydi.

Shu sababli demon ishga tushganda **pwm sinovi** o'tkazadi (qiymatni qisqa vaqtga
o'zgartirib, natijani o'qiydi) va ishlamasa qo'lda rejimni umuman taklif qilmaydi —
soxta "qo'lda" holat yaratilmaydi. Buning o'rniga termal rejim ishlatiladi.

### Termal rejim — BIOS darajasidagi qo'shimcha boshqaruv

(AWCC boost'dan mustaqil ishlaydi; ikkalasini birga ham ishlatsa bo'ladi.)

BIOS'ning `ThermalManagement` sozlamasi Linux'dan o'zgartiriladi
(`dell-wmi-sysman`, BIOS admin paroli qo'yilmagan bo'lsa). O'lchangan natija
(taxminan bir xil yuklamada, CPU ≈ 60–64 °C):

| Rejim | Kuler 1 | Kuler 2 |
|---|---|---|
| Quiet | 2028 RPM | 2214 RPM |
| Optimized (zavod) | 2072 RPM | 2287 RPM |
| Cool | 2185 RPM | 2493 RPM |
| UltraPerformance | 2509 RPM | 2821 RPM |

```bash
fanctrl profile                    # rejimlar ro'yxati va joriysi
fanctrl profile UltraPerformance   # maksimal sovutish
fanctrl profile Quiet              # jim rejim
fanctrl profile Optimized          # zavod sozlamasi
```

Bu sozlama BIOS'da saqlanadi — qayta yuklangandan keyin ham kuchda qoladi.
GUI'da yuqoridagi tugmalar qatori, panel menyusida esa "Termal rejim" bandi
xuddi shu ishni qiladi.

### pwm boshqaruvi ishlaydigan tizimlarda

Dastur universal: pwm sinovi muvaffaqiyatli o'tsa, qo'lda rejim, slayderlar va
`fanctrl set` ochiladi. U holda:

* *Qo'lda* rejim = demon kerakli pwm qiymatini davriy ravishda qayta yozib turadi;
* *Avto* rejim = demon yozishni to'xtatadi, boshqaruv BIOS'ga qaytadi;
* pwm ko'pincha uzluksiz emas (0/128/255 kabi pog'onalar) — aniq pog'onalarni
  `fanctrl calibrate` aniqlaydi va qiymatlar shunga tekislanadi.

## Batareya va quvvatlanish chegarasi

Noutbukni doim rozetkada ishlatish batareyani o'ldirmaydi — zamonaviy noutbuk
rozetkada turganda tokni to'g'ridan-to'g'ri adapterdan oladi, batareya
zaryadsizlanmaydi. Batareyani yemiradigan narsa — **doimiy 100% da turish**:
li-ion element to'la zaryadda yuqori kuchlanish ostida bo'ladi va sig'imini
sekin yo'qotadi. Yechim — quvvatni 60–80% da to'xtatish.

Dastur buni BIOS orqali qiladi (`dell-wmi-sysman` → `PrimaryBattChargeCfg`,
`CustomChargeStart`, `CustomChargeStop`). Sozlama firmware'da saqlanadi, ya'ni
Linux o'chirilganda ham, Windows'ga o'tilganda ham kuchda qoladi.

| Rejim | Ma'nosi |
|---|---|
| `Adaptive` | BIOS foydalanish odatingizga qarab o'zi tanlaydi (zavod) |
| `Standard` | Har doim 100% gacha quvvatlanadi |
| `Express` | Eng tez quvvatlanish — eskirish eng yuqori |
| `PrimAcUse` | Doim rozetkada: quvvat ~50% da ushlanadi, eskirish eng kam |
| `Custom` | Qo'lda chegara: `start` dan pastga tushsa quvvatlanadi, `stop` da to'xtaydi |

```bash
fanctrl battery                 # holat: foiz, salomatlik, joriy rejim
fanctrl battery PrimAcUse       # doim rozetkada ishlaydiganlar uchun
fanctrl battery custom 50 60    # 50% → 60% oralig'ida ushlab turish
fanctrl battery Standard        # odatdagi 100% gacha quvvatlashga qaytarish
```

Chegaralar: boshlanish 50–95%, tugash 55–100%, orasi kamida 5%.

**Salomatlik** (`charge_full / charge_full_design`) — batareyaning hozirgi
sig'imi zavoddagisiga nisbatan. GUI'da "Batareya" kartochkasida, panel menyusida
esa "Batareya" bandida ko'rinadi.

Chegara o'rnatilgach batareya joriy foizdan sekin pasayib chegaraga tushadi —
bu normal holat, quvvat "yo'qolgani" emas.

## Sozlash

`/etc/fanctrld.conf` ni tahrirlab, so'ng:

```bash
sudo systemctl restart fanctrld
```

Muhim kalitlar:

* `socket_group` — kulerni boshqarishga ruxsat etilgan guruh;
* `poll_interval` — sensorlarni o'qish oralig'i (standart 2 s);
* `min_manual_pwm` — qo'lda rejimda ruxsat etilgan eng past pwm (0 bo'lsa kulerni
  butunlay to'xtatish mumkin; masalan 60 qilib qo'ysangiz kuler hech qachon to'xtamaydi);
* `restore_on_start` — demon qayta ishga tushganda oxirgi qo'lda sozlamani tiklash.

## Diagnostika

```bash
systemctl status fanctrld
journalctl -u fanctrld -f          # demon loglari
fanctrl status                     # klient tomondan tekshirish
ls -l /run/fanctrld.sock           # soket va uning guruhi
sensors                           # lm-sensors bilan solishtirish
```

Tez-tez uchraydigan holatlar:

* `soket topilmadi` — demon ishlamayapti: `sudo systemctl start fanctrld`;
* `ruxsat yo'q` — `/etc/fanctrld.conf` dagi `socket_group` sizning guruhingiz emas;
* `pwm boshqaruvi topilmadi` — hwmon'da pwm fayli yo'q (dastur monitoring rejimida ishlaydi).

## Tuzilma

```
fanctrl/
  hwmon.py      sysfs qatlami: kulerlar, harorat sensorlari, nvidia-smi
  daemon.py     root demon: sikl, governor, unix soket serveri
  client.py     soket klienti
  protocol.py   JSON-qator protokoli
  config.py     /etc/fanctrld.conf o'quvchi
  cli.py        fanctrl buyrug'i
  awcc.py       AWCC boost (acpi_call orqali)
  thermal.py    BIOS termal rejimlari
  battery.py    batareya holati va quvvatlanish rejimlari
  i18n.py       tarjimalar
  gui.py        PyQt5 oynasi
  locales/      tarjima fayllari (<til>.json)
gnome-extension/fanctrl@nurbekliv.github.io/
  extension.js  GNOME 42+ panel indikatori (GJS, asinxron soket klienti)
  metadata.json, stylesheet.css
systemd/fanctrld.service
share/fanctrl.desktop
install.sh / uninstall.sh          demon + CLI + GUI (sudo kerak)
install-extension.sh               panel kengaytmasi (sudo kerak emas)
setup-hotkey.sh                    klaviatura yorlig'i (sudo kerak emas)
tools/i18n_check.py                tarjima shabloni va tekshiruvi
```

Tashqi bog'liqliklar: faqat `python3` (3.9+) va GUI uchun `python3-pyqt5`.
Kengaytma uchun qo'shimcha paket kerak emas.

## Nom haqida

Buyruq `fanctl` emas, `fanctrl` deb ataladi: `fanctl` nomi Ubuntu'da allaqachon band —
u `ubuntu-fan` paketiga (Docker/LXD uchun Fan Networking) tegishli va uni ustidan
yozib qo'yish LXD/Docker tarmog'iga xalaqit berishi mumkin edi.

## Til

Dastur matnlari inglizcha yozilgan, o'zbekcha tarjima `fanctrl/locales/uz.json` va
`gnome-extension/fanctrl@nurbekliv.github.io/locales/uz.json` fayllarida. Tizim tili
o'zbekcha bo'lsa (`LANG=uz_UZ.UTF-8`) interfeys avtomatik o'zbekcha chiqadi; majburan
tanlash uchun `FANCTRL_LANG=uz`. Yangi til qo'shish: `python3 tools/i18n_check.py --init <til>`
(batafsil — README.md'dagi "Adding a language" bo'limi).

## Litsenziya

[MIT](LICENSE) © Nurbek Amrulloyev
