# Обновление 0.4.0 — аппаратные журналы (15 ноября 2026)

Происхождение: `H:/2026/ChipTuning/mse6/workspace/2026-11-15_crank_48-1_1000rpm`
(разработка и аппаратные операции — в workspace; сюда перенесены журналы и
сводки; бинарные backup-образы и HEX остаются в workspace).

## Файлы

- `upload_20261115T184155Z.log` / `.json` — загрузка 0.4.0: avrdude, flash-only,
  13 378 байт записано и verified, fuses не менялись (E:00 H:00 L:00).
- `verify_after_flash_20261115T184236Z.jsonl` — проверки после прошивки:
  HELLO 0.4.0; автостарт после reset (`crank_running=true, rpm=1000`);
  `CRANK_CONFIG(4)` → BAD_ARGUMENT; `CRANK_CONFIG(3, invert=1)` и
  `CRANK_SET_RPM 1000` — echo OK; 10/10, ошибок нет.
- `preflash_compare.json` — сверка платы до записи: flash == опубликованный
  образ 0.3.0 (0 различий), EEPROM == `eeprom_after_v030.hex` (0 различий).
- `workspace_manifest.json` — манифест рабочей workspace задачи (хеши файлов).

Сводки версии: [`MANIFEST.json`](../../MANIFEST.json),
[`PROVENANCE.json`](../../PROVENANCE.json), раздел 0.4.0 в
[`HARDWARE_REPORT.md`](../../HARDWARE_REPORT.md).

Ограничения: подтверждение на уровне команд/статуса; форма сигнала ДПКВ
осциллографом не измерялась; ЭБУ/CAN/K-line/J2534 не использовались.
