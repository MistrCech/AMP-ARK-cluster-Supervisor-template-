# ARK: Survival Evolved Cluster — AMP template

Celý ARK cluster v **jedné** instanci AMP. Zaškrtneš mapy, které chceš, a Python
supervisor uvnitř je spustí nad **jednou** instalací hry se **sdílenou** konfigurací.

Řeší to bolest 13 samostatných instancí: 13 × 15 GB stejných souborů, update, při
kterém se musí všechny zastavit a každá přepsat zvlášť, a konfigurace, která se
mezi nimi postupně rozejde.

## Co to umí

- **13 map z jedné instalace** — ~15 GB místo ~195 GB, jeden SteamCMD zápis při updatu
- **Ovládání po mapách zůstává** — konzole AMP je příkazový kanál (`AdminMethod=STDIO`)
- **Postupný start** s prodlevou; 13 serverů naráz stroj neustojí
- **CPU pinning** — každá mapa vlastní fyzické jádro včetně SMT sourozence
- **Cross-server chat** — nahrazuje Cross-Ark-Chat, bez druhého procesu
- **Evolution eventy** (2×/4×) — přepnou se při nejbližším restartu
- **Restartové fixy per mapa** — `DestroyWildDinos`, úly, hnízda, vejce
- **Zálohy za běhu** — `quiesce`/`dequiesce`, doteď to uměl jen Minecraft
- **Seznam hráčů, chat a kick/ban tlačítka** v UI AMP

## Instalace

V AMP přidej repozitář (Configuration → Instance Deployment):

```
MistrCech/AMP-ARK-cluster-Supervisor-template-:main      produkce
MistrCech/AMP-ARK-cluster-Supervisor-template-:staging   testovani
```

Na hostiteli musí být **systémový Python** verze zadané v nastavení instance
(výchozí 3.11) — z něj se staví venv:

```bash
sudo apt install python3.11 python3.11-venv     # Debian/Ubuntu
```

Pak vytvoř instanci ze šablony **ARK: Survival Evolved (Cluster)**, zaškrtni mapy
v sekci *Maps*, **nastav RCON heslo** (bez něj supervisor mapy neovládá) a spusť Update.

## Příkazy v konzoli

```
status                prehled map: bezi/nebezi, PID, jadro, hraci
players               hraci po mapach
start|stop|restart <mapa>
broadcast <zprava>    na vsechny mapy
say <mapa> <zprava>
rcon <mapa> <prikaz>  |  rconall <prikaz>
saveall
kick|ban <hrac>       supervisor mapu dohleda sam
whereis <hrac>
verifyfixes [mapa]    over nazvy trid v mapfixes.json pres GetAll
event list|status|set <preset>|apply
quiesce | dequiesce
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

Proto supervisor `Game.ini` **generuje** ze šablony v tomhle repu a pak ho zamkne
na `chmod 444`. ARK si ten soubor jinak při vypnutí přepisuje vlastními hodnotami.

**Pozor na směr:** intervalové hodnoty se **snižují**. Napsat u „4×" všude `4.0`
by breeding čtyřnásobně *zpomalilo*.

## Konfigurace

Konfigurace je generovaná — needituj ji v instanci, přepíše se. Uprav místo toho:

| soubor | co |
|---|---|
| `supervisor/config/Game.ini.template` | stackování, zakázané spawny, rates |
| `supervisor/config/GameUserSettings.ini.template` | základní nastavení serveru |
| `supervisor/presets.json` | násobky eventů |
| `supervisor/mapfixes.json` | úklid před vypnutím, per mapa (`safe` / `destructive`) |

### Stackování

`ItemStackSizeMultiplier` funguje na většinu věcí, ale **kazící se maso ho ignoruje**.
Prime meat a mutton proto mají vlastní `ConfigOverrideItemMaxQuantity` s
`bIgnoreMultiplier=True`. U **muttonu** je hlášené, že to v některých verzích
nefunguje — ověř ve hře, ne jen v souboru.

### Restartové fixy

`DestroyWildDinos` **nemaže struktury**, které dinosauři postavili — proto se úly
hromadí právě tehdy, když se ten příkaz pouští často.

Úklid běží **před** vypnutím mapy, aby repopulace (5–10 min) proběhla při bootu,
kdy nikdo nehraje.

Úklid je rozdělený na dvě skupiny:

- **safe** — běží vždy. Maže jen divokou faunu a věci, které hráč nemůže vlastnit
  (hnízda, nesebraná divoká vejce).
- **destructive** — běží jen když zapneš *Destructive Restart Cleanup*
  (výchozí **vypnuto**). Sem patří `DestroyAll BeeHive_C`, protože zdroje si
  protiřečí v tom, jestli maže i **hráčské** úly. Postavený úl je ochočená Giant
  Queen Bee, takže špatný odhad znamená nevratnou ztrátu zvířete.

**`DestroyAll` nevrací žádný výstup** — ani při úspěchu, ani při překlepu v názvu
třídy. Log tedy dokazuje jen to, že příkaz dorazil na server. Novou třídu ověř
příkazem `verifyfixes`, který pro každou třídu pošle `GetAll` a spočítá výskyty.

Ledové wyverny jsou v šabloně **zakomentované**, a to ve variantě, která je
**nahradí** normální wyvernou místo aby je zrušila — hnízdní místa tak zůstanou
obsazená. Pozor: `Ragnarok_Wyvern_Override_Ice_C` používá **i Valguero**, a
`Game.ini` je sdílený, takže odkomentování zasáhne obě mapy.

## Porty

| | rozsah |
|---|---|
| Game | 7777 + 2×index (+1 peer) |
| Query | 27015 + index |
| RCON | 27100 + index |

Port se odvozuje z **kanonického** pořadí mapy, ne z pořadí spuštění — mapa má pořád
stejný port, i když jinou odškrtneš.

## Vývoj

`staging` na testování, `main` na produkci. Supervisor jde spustit i mimo AMP:

```bash
cd supervisor
ARK_BASE_DIR=/cesta/k/instanci/ ARK_RCON_PASSWORD=tajne \
  python3 arkclustersupervisor.py TheIsland Ragnarok
```
