# ARK Cluster — AMP template (ASE na Linuxu, ASA na Windows)

Celý ARK cluster v **jedné** instanci AMP. Zaškrtneš mapy, které chceš, a Python
supervisor uvnitř je spustí nad **jednou** instalací hry se **sdílenou** konfigurací.

Dvě šablony, jeden supervisor (hru vybírá proměnná `ARK_GAME`):

| šablona | hra | hostitel |
|---|---|---|
| **ARK: Survival Evolved (Cluster)** | ASE, 13 map | Linux |
| **ARK: Survival Ascended (Cluster)** | ASA, 10 map | Windows (ASA server pro Linux neexistuje) |

Řeší to bolest 13 samostatných instancí: 13 × 15 GB stejných souborů, update, při
kterém se musí všechny zastavit a každá přepsat zvlášť, a konfigurace, která se
mezi nimi postupně rozejde.

## Co to umí

- **13 map z jedné instalace** — ~15 GB místo ~195 GB, jeden SteamCMD zápis při updatu
- **Ovládání po mapách zůstává** — konzole AMP je příkazový kanál (`AdminMethod=STDIO`)
- **Postupný start** s prodlevou; 13 serverů naráz stroj neustojí
- **CPU pinning** — každá mapa vlastní fyzická jádra včetně SMT sourozenců, férový
  díl volných jader v souvislém bloku (když je map víc než jader, dělí se a
  supervisor to ohlásí)
- **Cross-server chat** — nahrazuje Cross-Ark-Chat, bez druhého procesu
- **Evolution eventy** (2×/4×) — přepnou se při nejbližším restartu
- **Restartové fixy per mapa** — `DestroyWildDinos`, úly, hnízda, vejce
- **Zálohy za běhu** — `quiesce`/`dequiesce`: AMP zálohuje bez vypnutí clusteru
  (ARK zapisuje save atomicky, viz níže; záloha je nanejvýš o autosave starší)
- **Seznam hráčů, chat a kick/ban tlačítka** v UI AMP — hráči z RCON `ListPlayers`
  i s ID, které kick/ban potřebují
- **Log každé mapy v konzoli AMP** — start, savy, chyby; řádek s hesly se nepouští
- **Korektní vypnutí** — úklid, pak SIGINT (ASE) nebo RCON `DoExit` (ASA); ARK při
  obojím uloží svět. Na Linuxu bez core dumpů. Na Windows jsou mapy v job objectu
  supervisoru: když ho AMP zabije, skončí i ony — žádní sirotci, které by příští
  start pustil podruhé nad stejným savem
- **Grafy v AMP** — mapy, hráči, RAM a CPU celého clusteru (AMP sám měří jen supervisor)

## Instalace

V AMP přidej repozitář (Configuration → Instance Deployment):

```
MistrCech/ArkClusterAMP:main      produkce
MistrCech/ArkClusterAMP:staging   testovani
```

Jméno repozitáře nesmí obsahovat pomlčku: AMP pozná adresář stažené šablony
jen podle `^(\w+)-(\w+)(-(\w+))?$` (vlastník-repo-větev) a jiný bere jako
zastaralý — smaže ho a šablony z něj nenačte (ADSModule 2.8). Proto se repo
jmenuje `ArkClusterAMP`, dřív `AMP-ARK-cluster-Supervisor-template-`.

Hostitel musí být **Linux** a mít **systémový `python3`** (aspoň 3.7) a `git`.
Supervisor běží na čisté standardní knihovně, takže žádný venv ani pip — stačí
Python, který na Debianu i Ubuntu je (Debian 13 má 3.13, Ubuntu 24.04 má 3.12).
Update to ověří v kroku *Python Check*. V kontejneru je obojí už v obrazu
`cubecoders/ampbase:debian`.

```bash
sudo apt install python3 git     # Debian/Ubuntu, pokud chybí
```

### ASA na Windows

Hostitel potřebuje **Python** (aspoň 3.7, instalátor z python.org „pro všechny
uživatele“) a **Git for Windows**, oba na **systémové** PATH — AMP běží jako
`NetworkService`, uživatelská PATH ho nezajímá. Cestu k `python.exe` zadáš v
nastavení instance (*Python Executable*, výchozí `C:\Program Files\Python313\python.exe`).

Pravidla Windows Firewallu si AMP pro porty instance dělá sám
(`AMP:<instance>:…`); na routeru přesměruj UDP herní a query porty (viz Porty).

### Obě hry

Pak vytvoř instanci ze šablony **ARK: Survival Evolved (Cluster)** nebo **ARK:
Survival Ascended (Cluster)**, zaškrtni mapy
v sekci *Maps* a spusť Update. RCON heslo můžeš nechat prázdné — supervisor si při
prvním startu vygeneruje vlastní a uloží ho do `supervisor-state.json` (práva 600),
kde ho najdeš pro externí RCON nástroje.

## Příkazy v konzoli

```
status                prehled map: bezi/nebezi, PID, jadro, hraci
players               hraci po mapach
start|stop|restart <mapa>
broadcast <zprava>    na vsechny mapy
say <mapa> <zprava>
rcon <mapa> <prikaz>  |  rconall <prikaz>
saveall               SaveWorld vsude (muze RCON map na minuty umlcet)
kick|ban <hrac|id>    supervisor mapu dohleda sam
whereis <hrac|id>
event list|status|set <preset>|apply
quiesce | dequiesce   zaloha za behu z AMP (save je atomicky)
DoExit                korektni ukonceni celeho clusteru
```

## Eventy

ARK čte multiplikátory **jen při startu** — živě je přepnout nejde. Event se proto
veze na ranním restartu:

```
event set 2x        nastavi preset, projevi se pri PRISTIM startu mapy
event apply         rolling restart hned, mapa po mape
```

Nebo přepni *Rate Preset* v nastavení instance. Pro opakující se víkendové eventy
nech scheduler AMP poslat `event set 2x` v pátek a `event set normal` v pondělí.

**Multiplikátory nežijí na jednom místě** a `presets.json` to respektuje:

| kde | co |
|---|---|
| příkazová řádka (`?Klic=hodnota`) | `XPMultiplier`, `TamingSpeedMultiplier`, `HarvestAmountMultiplier` — **a nic víc** |
| `Game.ini` — přes `?` to **nejde** | `EggHatchSpeedMultiplier`, `BabyMatureSpeedMultiplier`, `MatingIntervalMultiplier`, `BabyFoodConsumptionSpeedMultiplier`, `LayEggIntervalMultiplier`, `BabyCuddleIntervalMultiplier` |

Ověřeno proti tabulkám oficiální wiki: v `[ServerSettings]` se `CMD=yes` jsou
opravdu jen ty tři. Tabulka Game.ini **nemá sloupec CMD vůbec**, takže cokoli
odtud předané přes `?` se tiše zahodí a event by breeding nezměnil.

Proto supervisor `Game.ini` **generuje** ze šablony v tomhle repu při startu
clusteru a při každém restartu mapy (`restart`, `event apply`) a zamyká na
`chmod 444` (ve všech testech ho ARK nechal netknutý).

**Pozor na směr:** intervalové hodnoty se **snižují**. Napsat u „4×" všude `4.0`
by breeding čtyřnásobně *zpomalilo*.

## Konfigurace

Konfigurace je generovaná — needituj ji v instanci, přepíše se. Uprav místo toho:

| soubor | co |
|---|---|
| `supervisor/config/Game.ini.template` | stackování, zakázané spawny, rates |
| `supervisor/config/ServerSettings.ini` | `[ServerSettings]` — jde na **příkazovou řádku** každé mapy |
| `supervisor/presets.json` | násobky eventů |
| `supervisor/mapfixes.json` | úklid před vypnutím, per mapa (`safe` / `destructive`) |

`GameUserSettings.ini` supervisor **negeneruje**: ručně napsaný ARK celý zahodí a
vytvoří znovu s výchozími hodnotami (ověřeno — stackování ×10 se tak na server
nikdy nedostalo). Klíče `[ServerSettings]` proto jdou na příkazovou řádku, kde je
ARK převezme. Smí tam jen klíče se sloupcem CMD na wiki a hodnoty bez mezer.

### Stackování

`ItemStackSizeMultiplier` funguje na většinu věcí, ale **kazící se maso ho ignoruje**.
Prime meat a mutton proto mají vlastní `ConfigOverrideItemMaxQuantity` s
`bIgnoreMultiplier=True`. U **muttonu** je hlášené, že to v některých verzích
nefunguje — ověř ve hře, ne jen v souboru.

### Restartové fixy

`DestroyWildDinos` **nemaže struktury**, které dinosauři postavili — proto se úly
hromadí právě tehdy, když se ten příkaz pouští často.

Úklid běží **před** vypnutím mapy, aby repopulace (5–10 min) proběhla při bootu,
kdy nikdo nehraje. Mezi příkazy je **45 s rozestup** — těžký příkaz poslaný pár
sekund po jiném ARK nevezme a RCON pak minuty mlčí; s rozestupem prošly všechny
(ověřeno). Mapa se třemi příkazy se tak vypíná ~1,5 minuty, mapy paralelně.

Úklid je rozdělený na dvě skupiny:

- **safe** — běží vždy. Maže jen divokou faunu a věci, které hráč nemůže vlastnit
  (hnízda, nesebraná divoká vejce).
- **destructive** — běží jen když zapneš *Destructive Restart Cleanup*
  (výchozí **vypnuto**). Sem patří `DestroyAll BeeHive_C`, protože zdroje si
  protiřečí v tom, jestli maže i **hráčské** úly. Postavený úl je ochočená Giant
  Queen Bee, takže špatný odhad znamená nevratnou ztrátu zvířete.

**`DestroyAll` nevrací žádný výstup** — ani při úspěchu, ani při překlepu v názvu
třídy. Log tedy dokazuje jen to, že příkaz dorazil na server. Novou třídu ověř
**ve hře** v admin konzoli klienta (`cheat GetAll <Třída>`), než ji přidáš. Přes
RCON to nejde, `GetAll` tam nevrací nic. Příkazy v `mapfixes.json` jsou **bez**
prefixu `cheat` — ten RCON potvrdí, ale příkaz neprovede (ověřeno na v361.7).

Ledové wyverny jsou v šabloně **zakomentované**, a to ve variantě, která je
**nahradí** normální wyvernou místo aby je zrušila — hnízdní místa tak zůstanou
obsazená. Pozor: `Ragnarok_Wyvern_Override_Ice_C` používá **i Valguero**, a
`Game.ini` je sdílený, takže odkomentování zasáhne obě mapy.

## Co ASE (v361.7) dělá jinak, než by člověk čekal

Ověřeno na skutečném serveru, ne odvozeno — z toho vychází návrh supervisoru:

- **Příkazovou řádku utne u první mezery** a všechno za ní tiše zahodí. S
  `SessionName="… - Mapa"` uprostřed zmizelo i `ServerPassword` — server byl
  veřejný. Jméno proto jde **poslední** a bez uvozovek.
- **Hesla musí na příkazovou řádku.** Bez `ServerAdminPassword` se RCON port
  vůbec neotevře a `ServerPassword` jen z ini nechá server veřejný. Každý lokální
  uživatel je tak vidí v `/proc/*/cmdline` (obrana: `/proc` s `hidepid=2`).
- **Na stdout nepíše herní log** — jen dva řádky ze Steam API. Proto
  `-log=<Mapa>.log -forcelogflush` a supervisor ten soubor posílá do konzole.
- **RCON posílá každých 10 s `Keep Alive`** — timeout musí být na celou odpověď,
  ne na jeden `recv`, jinak čekání nikdy neskončí.
- **Prefix `cheat` RCON potvrdí, ale příkaz neprovede.** `GetAll` přes RCON
  nevrací nic.
- **SIGINT i RCON `DoExit` svět uloží a server do pár sekund ukončí — a hned
  potom spadne na SIGABRT** (pokaždé). Core dump by měl velikost RAM mapy, proto
  ho supervisor vypíná. K vypnutí používá SIGINT, protože funguje i tehdy, když
  RCON zrovna neodpovídá.
- **Save se zapisuje atomicky** — `.ark` dostane nový inode naráz, rozepsaný
  soubor nikdy neexistuje. Kopie za běhu je vždy celý soubor.
- **Po RCON `SaveWorld` (a jiných těžkých příkazech) RCON mapy občas na minuty
  ztichne** — nejen první mapy, i ostatních. Nepravidelné: v klidu prošly dva
  `SaveWorld` 40 s po sobě, na zatíženém stroji se RCON po rychlém druhém savu
  zastavil na obou mapách. Vše ukazuje na těžký příkaz krátce po jiném těžkém
  příkazu — s rozestupem 40–45 s prošly všechny (úklid ho proto má). Supervisor
  proto na RCON nestaví nic kritického: připravenost bere z logu, vypíná
  SIGINTem a quiesce `SaveWorld` neposílá.
- **Vyhladovělý server přestane obsluhovat i signály.** Se sníženou prioritou
  (`nice 19`, `CPUWeight=1`) a pinningem na zatížená jádra jednou nezareagoval
  ani na SIGINT. Mapy nespouštěj se sníženou prioritou.
- **S pinningem vidí UE jen přidělená jádra** (`Number of cores 1` v logu při
  jednom jádře) a podle toho dimenzuje pracovní vlákna. S jedním jádrem startuje
  TheIsland ~40 s; srovnání bez pinningu a dopad na tick při hráčích zatím
  změřené nejsou.

## Co ASA (v94.15) dělá jinak

Ověřeno na skutečném serveru (Windows, 3 mapy naraz), ne odvozeno:

- **„has successfully started!“ neznamená nic.** U nového světa ho ASA píše hned
  na začátku načítání (0,7 GB RAM, ~5 s po startu). Svět je načtený až s řádkem
  `Server has completed startup and is now advertising for join. (N GB Mem)`,
  1–2,5 min po startu. RCON port poslouchá od ~15 s, ale odpovídá až po `Full
  Startup`, 4–10 s před tím řádkem.
- **`?Port=` v URL ignoruje.** Mapa zkusí výchozí 7777, a když je obsazený, tiše
  vezme další volný — Ragnarok s `Port=7789` skončil na 7779. Herní port jen přes
  `-port=`. Peer port (herní + 1) ASA nepoužívá.
- **Steam subsystem chce vlastní query port** (ověření hráčů ze Steamu; servery
  se jinak hledají přes EOS). `?QueryPort=` v URL nebere — všechny mapy pak chtějí
  27015, uspěje jen první a ostatní píšou `Steam Subsystem initialized: FAILED`.
  Funguje jen přepínač `-QueryPort=`.
- **RCON odpovídá na každý příkaz právě jedním paketem**, i velkým (`GetGameLog`
  7,5 kB), a příkazy po jednom zvládá na jednom spojení. Na prázdný
  `RESPONSE_VALUE` — trik, kterým se u ASE pozná konec odpovědi — ale neodpoví a
  spojení pak mlčí úplně, i na další příkazy. Stejně dopadnou dva pakety v jednom
  čtení. Klient má proto pro ASA režim bez terminátoru.
- **`DoExit` odpoví `Exiting...`**, spojení nechá otevřené, svět uloží za necelou
  sekundu a proces skončí do 10–55 s — bez pádu (žádný crash dump ani chyba v
  protokolu aplikací, na rozdíl od ASE). Celé vypnutí clusteru (úklid, 45 s,
  `DoExit`) trvá ~80 s.
- **Afinitu si sám přepíše.** `SetProcessAffinityMask` po startu ASA vrátí na všechna
  CPU, drží až limit job objectu. Počet vláken UE stejně bere z celého stroje
  (`Number of cores 32`), proto pinning dává mapě férový díl jader, ne jedno.
- **Na stdout sype analytiku** (GameAnalytics) a každou minutu JSON s výkonem (FPS,
  ms herního vlákna), ~65 řádků za minutu na mapu, promíchaných mezi vlákny.
  Supervisor je zahazuje — herní log jde jako u ASE z `-log=<Mapa>.log`.
- **`SessionName` s mezerami projde** — Python argument na Windows uzavře do
  uvozovek (ověřeno „Sarkastic ASA Test - TheIsland_WP“).
- **RAM: 7–11 GB na mapu** po startu (ScorchedEarth 7, TheIsland 10, Ragnarok 10,8).

## Porty

| | ASE | ASA |
|---|---|---|
| Game (UDP) | 7777 + 2×index (+1 peer) | 7777 + 2×index |
| Query (UDP) | 27015 + index | 27015 + index (Steam subsystem) |
| RCON (TCP) | 27100 + index | 27100 + index |

Základní porty jdou změnit v AMP (Edit Instance, když instance stojí) — mapy se
od nich odvozují stejně. Na routeru přesměruj jen UDP herní a query porty. RCON
ne: supervisor se k němu připojuje přes localhost a AMP na Windows otevírá ve
firewallu hostitele všechny porty instance, takže RCON je i tak dostupný z LAN.

Port se odvozuje z **kanonického** pořadí mapy, ne z pořadí spuštění — mapa má pořád
stejný port, i když jinou odškrtneš.

## Vývoj

`staging` na testování, `main` na produkci. Supervisor jde spustit i mimo AMP:

```bash
cd supervisor
ARK_BASE_DIR=/cesta/k/instanci/ ARK_RCON_PASSWORD=tajne \
  python3 arkclustersupervisor.py TheIsland Ragnarok
```

ASA na Windows (`cmd`; hra v `<ARK_BASE_DIR>\2430930`):

```bat
cd supervisor
set "ARK_GAME=asa" & set "ARK_BASE_DIR=D:\arktest\asa\"
python -u arkclustersupervisor.py TheIsland_WP ScorchedEarth_WP Ragnarok_WP
```
