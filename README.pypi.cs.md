# mbmerge – popis balíčku pro PyPI

[English](README.pypi.md) | **Čeština**

Inkrementální slučování PNG a WebP rastrových MBTiles s alfa kanálem, omezenými dávkami, paralelním zpracováním obrázků a jediným SQLite writerem. Po přerušení lze bezpečně pokračovat bez opakovaného nanesení potvrzené poloprůhledné vrstvy.

## Instalace

```bash
pipx install mbmerge
mbmerge --version
```

Alternativně použij `python -m pip install mbmerge` ve virtuálním prostředí. Vyžaduje Python 3.11+, SQLite 3.35+ a Linux/macOS. Windows není podporován. Pillow se nainstaluje jako závislost; pro WebP vstup/výstup je potřeba podpora WebP.

## Použití

```bash
mbmerge merge --input-dir /data/inputs --output /data/merged.mbtiles
mbmerge merge --input-dir /data/inputs --output /data/merged-png.mbtiles \
  --output-format png --png-compress-level 3
mbmerge status --output /data/merged.mbtiles
mbmerge verify --output /data/merged.mbtiles --decode
```

Stejným příkazem navážeš po přerušení. Dokončené nezměněné zdroje se přeskočí a nové přidají. Pozdější zdroj se kreslí přes předchozí ve společných souřadnicích dlaždice. Odlišné souřadnice zůstávají samostatnými dlaždicemi ve společném archivu.

## Vstupy a provozní omezení

Vstupy musí být neměnné, offline a mít plochou tabulku MBTiles s unikátním vzestupným indexem na `(zoom_level,tile_column,tile_row)`. Souřadnice jsou Web Mercator/TMS. Dlaždice mají shodné čtvercové rozměry: výchozí je 512 px, pro 256 px zadej `--tile-size 256`. Podporuje se statické WebP a PNG do 8 bitů na kanál. Smíšené vstupy se převedou na jednotný výstup, výchozí je bezztrátové WebP. Animace, 16bitové PNG, JPEG, vektorové dlaždice, normalizovaná schémata a WAL zdroje se odmítají.

Výstup vytvořený mbmerge se rozšiřuje. Archiv vytvořený jiným nástrojem použij jako první vstup do nového výstupu. Změněné již evidované zdroje se odmítnou: obnov původní bajty nebo vytvoř nový archiv. Všechny evidované zdroje musí zůstat dostupné na původních cestách. Formát výstupu a aktivní kompresní parametry jsou při resume pevné.

Dávky, odeslané úlohy, bloby i rozměry jsou omezené, ale rozpočet dávky není tvrdý limit celkového RSS. Používej lokální disk se SQLite zamykáním a vyhni se dlouhým čtenářským transakcím nad výstupem. Bounds vycházejí ze zdrojových metadat nebo celých dlaždic, bez ořezu podle alfa kanálu. Reálný 1TB import zatím nebyl otestován.

Všechny volby zobrazí `mbmerge merge --help`. Podporuje se také `python -m mbmerge`.

## Licence

MIT. Copyright (c) 2026 mbmerge contributors. Závazné znění obsahuje LICENSE.md v distribuci.
