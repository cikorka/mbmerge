# mbmerge

[English](README.md) | **Čeština**

Obecné Python CLI pro inkrementální slučování rastrových PNG a WebP MBTiles do jednoho výstupního archivu. Zdroje se zpracovávají postupně, validace a skládání překryvů paralelně. Do výstupního SQLite zapisuje jediný hlavní proces.

**Požadavky:** Linux/macOS (POSIX), Python 3.11+, SQLite 3.35+, Pillow (podpora WebP je potřeba pro čtení nebo zápis WebP). Reálný 1TB archiv zatím nebyl otestován. Ověřené chování a naměřená paměť jsou v [protokolu ověření](TEST_REPORT.cs.md).

## Co se slučuje?

Výstup obsahuje sjednocení souřadnic dlaždic ze všech vstupů. Pokud dva zdroje přispívají sousedícími nebo překrývajícími se částmi obrazu **do stejného `z/x/y`**, jejich pixely se složí do jedné výsledné dlaždice. Různé souřadnice zůstanou samostatnými dlaždicemi ve společném archivu.

![Dva archivy přispívají do společné souřadnice dlaždice. Výstup zachová jedinečné dlaždice a složí tu společnou.](docs/images/tile-grid.png)

Následující obrázek používá skutečné přibalené 512 × 512px vstupy. Šachovnice znamená průhlednost. Pozdější zdroj se kreslí přes předchozí, včetně poloprůhledného překryvu.

![Skutečné demo: zdroj A, zdroj B a výsledek složení jejich pixelů ve stejné dlaždici.](docs/images/compositing.png)

## Instalace

Balíček, repozitář i příkaz se jmenují **mbmerge**. Po prvním zveřejnění na PyPI:

```bash
pipx install mbmerge
mbmerge --version
```

Před prvním zveřejněním nebo při práci ze zdrojů použij následující instalaci z repozitáře. Podporuje se i `python -m mbmerge`; původní spuštění `python merge_mbtiles.py` zůstává kompatibilní.

## Instalace ze zdrojů a rychlý test

V adresáři tohoto balíčku:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v

mbmerge merge \
  --input demo/source_a.mbtiles --input demo/source_b.mbtiles \
  --output demo/merged.mbtiles --workers 2 --name "Ukázka rastrových vrstev"
mbmerge verify --output demo/merged.mbtiles --decode
mbmerge status --output demo/merged.mbtiles
```

Repozitář obsahuje 512px vstupy `demo/source_a.mbtiles`, `demo/source_b.mbtiles`, referenční obrázek `demo/expected_overlap.png` a odpovídající PNG MBTiles v `demo/png/`. Příkazy výše vytvoří vlastní `demo/merged.mbtiles`. Výstup obsahuje v evidenci absolutní cesty, proto se předem vytvořený výstup nepublikuje. Souřadnice ukázkových dat jsou syntetické. Další sadu vytvoří:

```bash
python examples/create_demo.py --output-dir new_demo
# Nebo vytvoření PNG MBTiles:
python examples/create_demo.py --output-dir new_demo_png --tile-format png
python examples/create_readme_images.py --demo-dir demo --output-dir docs/images
```

## PNG a WebP

Pro PNG výstup použij `--output-format png`, pro WebP `--output-format webp`. Nový výstup je ve výchozím nastavení **WebP**, i když všechny vstupy obsahují PNG. Při resume se bez tohoto přepínače použije evidovaný formát existujícího výstupu. PNG a WebP vstupy lze kombinovat; všechny výsledné dlaždice budou ve zvoleném formátu a `metadata.format` tomu odpovídá.

```bash
# PNG vstupy do PNG výstupu, včetně průhledných překryvů:
mbmerge merge \
  --input-dir demo/png --output demo/png-merged.mbtiles \
  --output-format png --png-compress-level 3

# PNG vstupy do bezztrátového WebP:
mbmerge merge \
  --input-dir demo/png --output demo/webp-from-png.mbtiles \
  --output-format webp
```

Úroveň komprese PNG je 0–9 (výchozí 6); nižší hodnota je obvykle rychlejší a vytváří větší soubor, bez změny dekódovaných pixelů. Platí pro nově kódované dlaždice. Jedinečné dlaždice již ve zvoleném formátu zachovají původní bajty. Ostatní se převedou; překryvy se vždy složí a zakódují do formátu výstupu. Kódování neobnoví ztráty přítomné ve zdroji.

Podporuje se statické PNG do 8 bitů na kanál: paletové s průhledností, odstíny šedi, šedá+alfa, RGB a RGBA. Animované PNG/WebP a 16bitové PNG se odmítají; 16bitový vstup nejdřív výslovně převeď na 8bitovou zobrazovací reprezentaci. Deklarovaný formát zdroje musí odpovídat obsahu dlaždic. Pokud `metadata.format` chybí, formát se rozpozná ze signatur jednotlivých dlaždic.

Formát výstupu je po dobu existence archivu pevný. Pro změnu vytvoř nový výstup z původních zdrojů. Při resume musí odpovídat také aktivní nastavení kodéru (`--webp-method` nebo `--png-compress-level`). Výstupy verze 1.0 zůstávají WebP; verze 1.1 převede jejich evidenci ze schématu 1 na 2 při nejbližším úspěšném commitu dávky nebo finálních metadat. Potom používej verzi 1.1+: verze 1.0 schéma 2 záměrně odmítne.

## Přepíše se existující výstup?

**Výstup vytvořený tímto CLI se rozšiřuje, nemaže se a nezačíná znovu.** Stejný příkaz ověří a přeskočí dokončené vstupy. Nové vstupy se přidají a překryvy se složí s dosavadním obrazem. Změněný již importovaný vstup se bezpečně odmítne před přidáním nových dlaždic. Přepínač `--resume` není potřeba.

MBTiles vytvořené jiným nástrojem neobsahuje evidenci importů tohoto CLI. Při použití jako `--output` se odmítne bez převzetí nebo nahrazení jeho dat. Pro sloučení ho uveď jako první vstup a zvol nový výstup:

```bash
mbmerge merge \
  --input existing.mbtiles --input additional.mbtiles \
  --output merged.mbtiles

# Další běhy mohou řízený výstup rozšiřovat:
mbmerge merge --input next.mbtiles --output merged.mbtiles
```

Původní archivy zůstanou zachované a vznikne historie potřebná pro bezpečné pokračování po pádu. Je potřeba místo na nový výstup. Výstup nesmí zároveň být vstupem, mít hardlinky ani odpovídat vstupnímu globu.

## Pilot a větší sady dat

Nejdřív připrav menší reprezentativní sadu neměnných překrývajících se zdrojů v `/data/pilot/inputs`. Výstup dej mimo vstupní adresář:

```bash
mbmerge merge \
  --input-dir /data/pilot/inputs --output /data/pilot/merged.mbtiles \
  --workers 4 --max-inflight 8 --batch-size 128 --batch-mib 32 \
  --cache-mib 32 --source-cache-mib 8 \
  --name "Raster mosaic" --type overlay \
  --json-log 2> /data/pilot/merge.jsonl
```

**Resume je automatické.** Po Ctrl-C, SIGTERM, SIGKILL či restartu spusť stejný příkaz. Přepínač `--resume` není potřeba. Exit kódy: 0 úspěch, 1 provozní chyba, 2 chybné argumenty, 130 obsloužené přerušení. Aktuální necommitnutá dávka se při chybě vrátí zpět.

Po pilotu lze stejný výstup rozšířit úplnou sadou, pokud se zachovají absolutní cesty původních vstupů. Kopie pilotních vstupů pod jinými cestami jsou nové vrstvy; pro nezávislý rebuild použij nový výstup. Zkontroluj překryvy a okraje zdrojů, RAM všech procesů, rychlost a volné místo. Výkon syntetického testu nelze přímo přepočítat na 1 TB.

## Pořadí a přidávání zdrojů

1. Opakované `--input` v zadaném pořadí.
2. Položky `--input-list` v pořadí řádků.
3. Soubory `--input-dir` lexikograficky podle absolutní cesty. Výchozí vzor je `*.mbtiles`, pro podadresáře lze použít `--pattern '**/*.mbtiles'`.

Pozdější zdroj se skládá **nad** dosavadním obrazem pomocí `Image.alpha_composite`. Poloprůhledné pixely se mísí, neprůhledné mají prioritu posledního zdroje. Historická priorita je uložená v databázi. Nový soubor se připojí na konec i s abecedně dřívějším názvem. Rozpracovaný zdroj se vždy dokončí před novými, i pokud ho další příkaz neuvede.

Stejná cesta v jednom příkazu se deduplikuje. Dva hardlinky téhož vstupu se odmítnou. Stejný obsah ve dvou nezávislých souborech se automaticky nededuplikuje.

UTF-8 `--input-list` obsahuje jeden soubor na řádku. Prázdné řádky a komentáře začínající `#` se ignorují. Relativní cesty jsou vůči adresáři seznamu, mezery uvnitř názvu zůstanou zachované. Nepodporují se názvy s novým řádkem nebo krajními mezerami.

```bash
mbmerge merge \
  --input-list /data/inputs.txt --output /data/merged.mbtiles --workers 4

# Pozdější nový zdroj:
mbmerge merge \
  --input /data/new/source_next.mbtiles --output /data/merged.mbtiles
```

## Paměť a backpressure

Worker procesy vznikají přes `spawn`; dostávají pouze bloby a parametry, žádná SQLite spojení. SQLite otevřená v hlavním procesu se do workerů nedědí.

- Zdroj se stránkuje pomocí indexované podmínky `(z,x,y) > poslední_klíč`, bez `OFFSET`. Nejprve se čtou jen klíče a délky blobů.
- Překryvy se hledají dávkovým `JOIN` s malou dočasnou tabulkou. Pořadí joinu je vynucené tak, aby se neprocházel celý výstup pro každou dávku.
- `--batch-mib` zahrnuje zdrojové bloby, staré bloby a rezervu výstupu: pro překryvy nebo možný převod formátu celý `--max-tile-mib`. Jedinečné dlaždice deklarované ve zvoleném formátu rezervují původní délku; worker deklaraci ověří a jejich bajty zkopíruje. I malý komprimovaný vstup tak má rezervu na větší lossless výsledek.
- Odeslaných úloh, včetně hotových neodebraných výsledků, je nejvýše `--max-inflight`. Další se odesílají až po odebrání výsledků. Nepoužívá se neomezený `executor.map`.
- Výsledky se ukládají omezenými `executemany` dávkami. Zdrojová dávka, její metadata a checkpoint se potvrzují v jedné transakci.
- Blob má limit délky, obrázky mají limit rozměrů. PNG IHDR i VP8X/VP8/VP8L se kontrolují před dekódováním; u PNG se navíc ověřují CRC chunků a statická struktura. Animace se odmítají. SQLite mmap je vypnutý, cache explicitně omezená, dočasné tabulky mohou přetéct na disk.

| Parametr | Výchozí hodnota | Význam |
|---|---:|---|
| `--output-format` | WebP pro nový výstup | `png` nebo `webp`; při resume se načte z evidence |
| `--png-compress-level` | 6 | úroveň komprese PNG 0–9, bez změny kvality pixelů |
| `--workers` | nejvýše 4 podle CPU | procesy pro validaci a compositing |
| `--max-inflight` | 2 × workers | maximum odeslaných úloh |
| `--batch-size` | 128 | maximum klíčů na transakci |
| `--batch-mib` | 32 | kódovaná data včetně rezervy výstupu |
| `--max-tile-mib` | 4 | maximum zdrojového, starého i výsledného blobu |
| `--sql-batch-size` | 16 | maximum řádků jednoho executemany |
| `--sql-batch-mib` | 4 | SQL buffer; jeden větší povolený blob se zapíše samostatně |
| `--cache-mib` | 32 | cache výstupního SQLite |
| `--source-cache-mib` | 8 | cache aktuálního vstupu |
| `--tile-size` | 512 | čtvercová dlaždice, nejvýše 4096 px |
| `--webp-method` | 4 | rychlost komprese 0–6, vždy lossless |
| `--busy-timeout` | 5 s | čekání na SQLite zámek/checkpoint |
| `--log-interval` | 5 s | interval progressu, dokončení zdroje se loguje vždy |

`--batch-mib` musí být alespoň trojnásobek `--max-tile-mib`, aby se vešel i maximální pár a výstup. Příklad úspornějšího 512px běhu:

```bash
mbmerge merge \
  --input-dir /data/inputs --output /data/merged.mbtiles \
  --workers 2 --max-inflight 2 --batch-size 64 \
  --max-tile-mib 2 --batch-mib 12 --cache-mib 16 --source-cache-mib 4
```

Rozpočet dávky **není pevný limit celkového RSS**. Nad něj existují Python/Pillow procesy, serializace, kopie blobů, dekódované obrazy a pracovní paměť libwebp. Paměť roste přibližně s `batch_mib + max_inflight × max_tile_mib + workers × tile_size²`, cache a režiemi procesů; neroste s počtem všech dlaždic. Pro absolutní provozní limit lze navíc použít cgroup/systemd `MemoryMax` na Linuxu. Po OOM se pokračuje od potvrzené dávky. Seznam vstupních cest je v RAM, obsah všech dlaždic není.

Každá dlaždice se dekóduje kvůli validaci. Bez překryvu se ve shodném formátu uloží **původní bajty**, bez rekomprese. Ostatní dlaždice se kódují jako bezztrátové PNG nebo WebP (`lossless=True, exact=True`). Původní ztrátovou kompresi tím nelze napravit. Další zdroj může stejnou nízkozoomovou dlaždici znovu dekódovat a zakódovat; tato verze upřednostňuje inkrementální import před dvoufázovým seskupením všech zdrojů.

## Transakce, pád, WAL a disk

Výstup obsahuje standardní `tiles`, `metadata` a pomocné `_merge_*` tabulky. Evidence uchovává absolutní cestu, SHA-256, informativní stat fingerprint, pořadí, stav, poslední potvrzený klíč, počty a rozsahy. Konfigurace uchovává verzi schématu a nastavení výsledku.

Dlaždice dávky, její metadata i poslední klíč se commitnou společně. Pád před commitem vrátí celou dávku, pád po commitu ji při resume přeskočí. To je zásadní pro poloprůhledné vrstvy: opakovaný compositing není idempotentní. Stav `active` se změní na `complete` až po finální kontrole SHA-256.

Výstup používá WAL, `synchronous=FULL` a na podporovaných systémech také `fullfsync`/`checkpoint_fullfsync`. Po registraci zdroje, každé dávce a dokončení proběhne `wal_checkpoint(TRUNCATE)`. Pokud ho blokuje externí čtenář, import **zastaví**, aby se WAL nehromadil přes další dávky. Zavři čtečku a pokračuj stejným příkazem. `journal_size_limit=0` sám o sobě není tvrdý limit WAL. WAL zahrnuje také SQLite stránky a indexy, nejen bloby.

Nedrž během importu dlouhé čtenářské transakce nad výstupem. Kooperativní zámek `*.merge.lock` brání dvěma instancím CLI. Soubor po skončení zůstává a **nesmí se za běhu mazat**. Pomocné tabulky ani dlaždice neupravuj mimo CLI.

Použij lokální disk a filesystem podporující SQLite WAL; síťové sdílení/NFS není podporované. Počítej s místem na výstup, dávkový WAL a rezervu. Nevzniká automatický `VACUUM` ani celá pracovní kopie TB archivu. Publikuj/zálohuj zavřený výstup po úspěšném checkpointu; nekopíruj jen hlavní MBTiles, pokud ještě existuje nevyprázdněný WAL. Odolnost vůči výpadku napájení závisí i na korektním fsync disku a filesystemu. Průběžný výstup obsahuje pouze potvrzenou část rozpracovaného zdroje.

## Změněné vstupy a bezpečná obnova

Vstupy musí být **offline, uzavřené a neměnné**. Požaduje se plochá tabulka `tiles` a unikátní vzestupný BINARY index na `(zoom_level,tile_column,tile_row)` v tomto pořadí. Deduplikované `map/images`, pohledy a zdroje v režimu WAL se odmítají. Exportuj je nejdřív do plochého MBTiles. Pro uzavřený WAL vstup proveď offline checkpoint a přepnutí na `journal_mode=DELETE`; **WAL s daty se nikdy ručně nemaže**.

Před každým během se vypočítá SHA-256 **všech evidovaných zdrojů**, i když je nový příkaz neuvádí. Nový/aktivní vstup se hashuje také před čtením a po dokončení. To stojí další sekvenční I/O, ale odhalí změnu s původní velikostí a mtime. Kontrola pouze podle stat se záměrně nenabízí. U velkého souboru může hashování trvat dlouho před prvním hlášením o dlaždicích.

Po dobu hashování a importu zůstává otevřená SQLite read transakce, která brání běžnému souběžnému SQLite zápisu. Při dávkách se kontroluje zařízení, inode, velikost, `mtime_ns`, `ctime_ns` a journal/WAL sidecary. Změna souboru zjištěná během importu označí výstup `tainted`; musí se vytvořit nový. Úmyslné obcházení zámků a libovolné přímé souběžné zápisy do bajtů souboru nejsou součástí kontraktu. Používej neměnné vstupní kopie/snapshoty.

Jiný SHA-256 dříve evidovaného vstupu při preflightu zastaví běh **před novými dlaždicemi**. Obnov původní přesné bajty na evidované cestě, nebo vytvoř nový výstup. Pouhé překreslení změněného zdroje by ponechalo staré odstraněné pixely. Tato verze proto nepodporuje výměnu/odstranění dřívější vrstvy, přesun evidovaných cest ani `--force-changed`. Uchovávej zdroje i po jejich importu. Samotná preflight neshoda SHA výstup netaintuje, takže obnovení přesných původních dat umožní resume.

## TMS, formáty obrázků a metadata

Vstupy jsou Web Mercator/TMS: řádek 0 je na jihu. Zoom 0–30, celé `x,y` v `[0,2^z)`. CLI nepřevrací souřadnice ani pixely. Chybějící `scheme` znamená předpoklad TMS, explicitní XYZ se odmítne. Chybně označený CRS nebo TMS nelze automaticky poznat. Přijímají se statické PNG a WebP dlaždice se stejným rozlišením podle pravidel výše; RGB je plně neprůhledné. JPEG, animace, 16bitové PNG a PBF se odmítají.

Metadata se aktualizují atomicky s dávkou i na konci:

- `minzoom/maxzoom`: ze skutečně potvrzených klíčů, bez přebírání deklarovaných zoomů. Používá se malá evidence zdrojů, ne opakovaný sken TB tabulky.
- `bounds`: sjednocení validních zdrojových bounds; při chybějících/nevalidních hodnotách rozsah importovaných celých TMS dlaždic. Číselně validní bounds se považují za důvěryhodné. Rozpracovaný zdroj s bounds uvádí po první dávce celý svůj deklarovaný rozsah.
- Přechod antimeridiánu se konzervativně rozšíří na `[-180,180]`. Přesné oříznutí podle alfa kanálu se nepočítá. Plně průhledné uložené dlaždice přispívají do zoomu a fallback rozsahu.
- `center`: střed bounds, zoom = minzoom, nikoli optimalizovaný viewport.
- `format=png` nebo `webp` podle zvoleného výstupu, `scheme=tms`, `version=1.3`, `type=overlay` nebo `baselayer`.
- `name`, `description`, `type` lze zadat i při dalším spuštění, neuvedené hodnoty se zachovají. `--tile-size`, formát výstupu a aktivní nastavení kodéru musí odpovídat původnímu běhu.
- `attribution`: unikátní celé texty, abecedně, oddělené ` | `. Max. 64 KiB na zdrojovou hodnotu a 1 MiB na výslednou atribuci. Prázdné zdroje nepřispívají k bounds, zoomům ani atribuci.

## Kontroly a opakování měření

```bash
# Jen malá evidence a metadata:
mbmerge status --output /data/merged.mbtiles

# quick_check prochází databázi; na TB může trvat dlouho:
mbmerge verify --output /data/merged.mbtiles

# Plná kontrola indexů + dekódování každého obrázku, ještě pomalejší:
mbmerge verify --output /data/merged.mbtiles \
  --full-sqlite-check --decode

# Současný součet RSS hlavního procesu a všech potomků; potřebuje ps.
# Asi 1.2 GiB dočasného disku, DB se po skončení smažou:
python tests/benchmark_memory.py --tiles 64 512 --work-dir /data/tmp > benchmark.local.json
# Opakování pro PNG výstup:
python tests/benchmark_memory.py --tiles 64 512 --output-format png --work-dir /data/tmp > benchmark-png.local.json
```

`verify` kontroluje výstupy spravované tímto CLI. Neporovnává obsah se zdroji a neověřuje jejich hashy. Uvádí počet nedokončených zdrojů. Hashy se ověřují při `merge`, i bez nových souborů. Testy pokrývají překryvy a průhlednost, metadata a TMS, pořadí, nezměněné bajty nových dlaždic, limity front/dávek, změněný obsah při stejné velikosti/mtime, poškozené obrázky, rozměry před dekódováním, chybu workeru po SQL zápisech, blokovaný checkpoint, dva CLI writery převody formátů a validaci PNG, migraci starší evidence a skutečný SIGKILL před/po commitu pro oba formáty výstupu.

Podklady: [SQLite WAL](https://www.sqlite.org/wal.html), [SQLite PRAGMA/synchronous](https://www.sqlite.org/pragma.html), [Pillow formáty obrázků](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html), [MBTiles 1.3](https://github.com/mapbox/mbtiles-spec/blob/master/1.3/spec.md).

## Vydávání a PyPI

[RELEASING.cs.md](RELEASING.cs.md) popisuje verzování, jednorázové propojení PyPI/TestPyPI a workflow GitHub Actions. Commity/PR spouštějí testy a kontrolu distribucí. Na PyPI publikuje pouze zveřejněný stabilní GitHub Release; TestPyPI má ruční workflow. Soukromý repozitář ani chybějící výslovné zapnutí publikace na PyPI/TestPyPI nepublikují. Tag musí přesně odpovídat `v` a jediné hodnotě `VERSION` v `merge_mbtiles.py`.

## Architektura a údržba

[ARCHITECTURE.cs.md](ARCHITECTURE.cs.md) obsahuje mapu kódu, stavový automat zdrojů, hranice transakcí, účtování paměti a pravidla rozšiřování. Komentáře a docstringy funkcí jsou anglicky. CLI i testy mají hlavičku `SPDX-License-Identifier: MIT`.

## Co patří do Gitu?

Verzuj zdrojové Python soubory, testy, příklady, dokumentaci a obrázky, syntetické vstupy `demo/source_*.mbtiles` a `demo/png/source_*.mbtiles` a připravené protokoly ověření. Komentovaný [.gitignore](.gitignore) vylučuje ostatní MBTiles/SQLite databáze, vytvořené výstupy, místní datové adresáře, logy, virtuální prostředí, cache a místní konfigurační soubory. Již sledované soubory z Gitu neodstraní. Pokud přidáš `.env.example`, nesmí obsahovat přístupové údaje.

## Licence

Projekt je zveřejněn pod [licencí MIT](LICENSE.md). Copyright (c) 2026 mbmerge contributors. Dostupný je i [informativní český překlad](LICENSE.cs.md); rozhodující je anglické znění licence.
