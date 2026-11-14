# Nano v1 — протокол и контракт

Версия прошивки 0.3.0, wire protocol 1. Это контракт реализации рабочего стенда, а не подтверждение совместимости с ЭБУ. Все целые поля многобайтовые — little-endian. Для точных ограничений и команды смотрите также реализацию в `firmware/bench/bench.ino` и `host/bench_client.py`.

## UART и кадрирование

UART 1 000 000 бод, 8N1 по умолчанию; резервная сборка — 500 000 бод. 921 600 не поддерживается на Nano 16 MHz: USART фактически работает около 1 MHz, расхождение +8.507% относительно 921600. В UART нет ASCII/debug-текста.

Сырой кадр: заголовок `<BBHBB` (version=1, type, seq, command, payload_length), payload, затем CRC16 little-endian. CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, без отражения, xorout 0; CRC покрывает заголовок и payload. Кадрирование: `COBS(raw) + 0x00`. Максимум payload 48 байт, raw 56 байт, wire 58 байт. Типы: 1 request, 2 response, 3 event. Запросы/ответы используют seq 1..65535, события — seq 0. Ответ соответствует команде и seq запроса; его payload начинается со status:u8. Событие не содержит status.

Хост поддерживает не более одного незавершённого запроса. Ответы приоритетнее телеметрии. На AVR ограниченный RX с межбайтовым таймаутом 20 мс; после переполнения/таймаута хвост отбрасывается до разделителя. Ошибочные CRC, тип, версия и seq отбрасываются и учитываются; на повреждённый кадр ответа нет. Корректный неизвестный opcode получает UNKNOWN_COMMAND. События помещаются в общую FIFO на 4 кадра; TX не блокирует loop, переполнение считается потерей.

Кэшируются последний полный запрос и ответ. Точный повтор того же wire/seq возвращает прежний ответ и не повторяет операцию. Тот же seq с иным содержимым отвечает BAD_ARGUMENT и не меняет кэш. Кэш не является историей сессии. HELLO — обычный запрос: при успешном исполнении обновляет кэш, но не сбрасывает конфигурации, выходы, потоки, генератор или ADC-счётчик. Во время отложенной операции другой запрос может получить BUSY; повтор идентичного выполняемого запроса ждёт исходный ответ. Клиент начинает с случайного ненулевого seq. Таймауты хоста: обычно 1 с, scan 2 с. Повтор ограничен одной попыткой по умолчанию и допускается только тем же кадром/seq. После неясного таймаута ADC_READ нельзя автоматически продолжать с новым seq: результат первого измерения мог быть выполнен.

Статусы: 0 OK, 1 UNKNOWN_COMMAND, 2 BAD_LENGTH, 3 BAD_ARGUMENT, 4 UNKNOWN_DEVICE, 5 DEVICE_NOT_RESPONDING, 6 BUS_ERROR, 7 BUS_TIMEOUT, 8 BUSY, 9 NOT_CONFIGURED. Ошибка может возвращать только status.

## Устройства, шины и пины

| ID | Устройство | Шина / адрес | Максимальный код | Примечание |
|---|---|---|---:|---|
| 0 | Плата | internal | — | board |
| 1 | TPL0501 | SPI | 255 | запись кода; readback отсутствует |
| 2 | ADS1115 | I2C, default 0x48 | 32767 | ALERT/RDY — D3 |
| 3 | MCP4725 | I2C, default 0x60 | 4095 | конфигурируемый адрес |
| 4 | MCP4725 | I2C, default 0x61 | 4095 | конфигурируемый адрес |
| 5 | Native ADC Nano | internal | 1023 | A6/A7, AVCC |

Kind: 1 TPL0501, 2 ADS1115, 3 MCP4725, 4 native ADC. Bus: 0 internal, 1 SPI, 2 I2C. Presence: 0 unknown, 1 ACK, 2 no ACK, 3 configured/unverifiable, 4 built-in. I2C ACK не подтверждает модель устройства. TPL0501 не имеет readback и не считается обнаруженным; SET_OUTPUT подтверждает только цифровую запись кода. Состояние выходов после reset — unknown; в setup нет записи в выходы или EEPROM.

I2C: SDA A4, SCL A5, 400 kHz, timeout Wire 3000 мкс с восстановлением через API. SPI TPL: D10 /CS (idle HIGH), D11 MOSI, D13 SCK, mode 0, MSB first, 8 MHz. Crank: D8 и опционально инвертированный D9. D2 marker переключается ровно один раз после успешного SET_OUTPUT. UART — D0/D1. ADS ALERT/RDY — D3, pull-up, INT1/FALLING. A6/A7 — native ADC. Генератор меняет только PB0/PB1; не перезаписывает весь PORTB. В setup генератор остановлен; SPI запускается только при SET_OUTPUT. EEPROM не используется.

## Команды

Во всех ответах ниже поля перечислены после status.

| Opcode | Команда | Запрос | Ответ |
|---:|---|---|---|
| `01` | HELLO | пустой | `<BBBIBB`: версия прошивки 0.3.0, capabilities:u32, max_payload=48, device_count=5. Биты capabilities 0..7: crank, ADS, TPL, DAC, ADS stream, marker, native ADC, native stream. |
| `02` | GET_STATUS | пустой | `<IBHBIIII`: uptime_us, crank_running, rpm, stream_running (любой ADC stream), rx_errors, tx_dropped, adc_missed, i2c_errors. |
| `03` | LIST_DEVICES | пустой | count:u8, затем count записей `<BBBBHB`: id, kind, bus, address, max_code, presence. |
| `04` | I2C_SCAN | пустой | count:u8 + адреса 7-bit. Проверяются 0x08..0x77 инкрементально; при 47-м найденном адресе BAD_ARGUMENT (максимум 46). Не пишет регистры. ADS stream запрещает scan (BUSY); native stream не запрещает. Генератор не останавливается. |
| `05` | GET_DEVICE_CONFIG | id:u8 | id + конфигурация: ADS `<BBBB>` address, channel 0..3, gain 0..5, rate 0..7; DAC — address; TPL — пусто; native ADC — channel 6 или 7. |
| `06` | SET_DEVICE_CONFIG | id + конфигурация GET_DEVICE_CONFIG | id + сохранённая конфигурация. Конфигурация программная и не означает обнаружение устройства. ADS получает её при ADC_READ/STREAM_START, не при SET. ADS/DAC нельзя менять при конфликтующем ADC/scan/stream; адреса не могут совпадать. Native channel — параметр one-shot, не потоковый. Native config допускается во время ADS stream, если native stream не активен и нет другой отложенной операции. |
| `10` | SET_OUTPUT | `<BH>` id,value | `<BHI>` id, applied_value, applied_time_us. TPL 0..255, DAC 0..4095. DAC — volatile fast-write, не EEPROM. Marker после успешной операции. |
| `11` | GET_OUTPUT | id:u8 | `<BBHI>` id, known, last_value, last_applied_us. Только программная тень TPL/DAC, не readback; для ADS — BAD_ARGUMENT. |
| `20` | ADC_READ | id:u8, 2 или 5 | `<BBIIhB>` id, channel, sample_number:u32, timestamp_us:u32, value:int16, flags:u8. Новое one-shot измерение по текущей конфигурации. Запрос устройства, которое уже стримит, получает BUSY; другой ADC можно читать. |
| `21` | ADC_STREAM_START | ADS: id=2; Native: id=5 либо `<BBI>` id=5, mask, period_us | ответ `id:u8`. Для ADS — описанная ниже ALERT-семантика. Native: пустой payload после id повторяет последние параметры; подробности ниже. |
| `22` | ADC_STREAM_STOP | id:u8, 2 или 5 | id:u8. Native STOP затрагивает только native scheduler; ADS STOP отключает поток MCU/IRQ и один раз посылает ADS команду idle. Уже буферизованные события могут прийти после ответа STOP. |
| `30` | CRANK_CONFIG | `<BB>` pattern_id, invert_output 0/1 | те же поля. Только при остановленном генераторе. Справочные pattern 0=60-2, 1=36-1, 2=24-1; не Voge. D8 raw, D9 инверсия только при включённой опции, иначе LOW. |
| `31` | CRANK_SET_RPM | rpm:u16 | rpm:u16. 10..20000; 0 останавливает. Timer1 CTC, ограниченный ISR; проверяется допустимость периода/prescaler. |
| `32` | CRANK_STOP | пустой | пустой. Идемпотентен, отключает timer IRQ и ведёт D8/D9 в LOW. |

ADS defaults: address 0x48, channel 0, gain 0 (±6.144 V), rate 7 (860 SPS). Rates 0..7: 8, 16, 32, 64, 128, 250, 475, 860 SPS. ADS адреса 0x48..0x4B. DAC адреса 0x60..0x67. Native one-shot default channel 6, AVCC reference, ADC prescaler 128 (125 kHz).

## ADC: ADS1115 и native

### ADS1115 (id 2)

Однократное чтение асинхронно проверяет idle по OS, переводит ADS в MODE=1/OS=0 и начинает новую конверсию; фиксированной задержки 130 мс и блокирующего ожидания нет. Поток — непрерывный один настроенный канал; требуется D3 ALERT/RDY. Comparator настраивается как conversion-ready (HI=0x8000, LO=0x0000, queue=00, polarity low, non-latching), INT1 срабатывает по FALLING. ISR минимален; отметка времени — готовность, полученная из IRQ либо polling approximation. Stream START асинхронно дожидается idle и подтверждает запись continuous config, не первого sample. Watchdog при отсутствии ALERT останавливает stream, увеличивает adc_missed и выполняет одну попытку перевода ADS в idle. Ошибка STOP возвращается и физическое состояние ADS помечается неизвестным; фоновых повторов нет. ADC flags bit0 — timestamp от ALERT, bit1 — missed/coalesced.

### Встроенный ADC Nano (id 5)

One-shot ADC_READ5 возвращает id=5, канал 6/7, собственный native counter, timestamp, raw code 0..1023 и flags=0. При активном native stream отвечает BUSY. ADSC проверяется без блокирующего ожидания, ADC ISR/free-running не используется; при первом чтении/смене MUX первая конверсия отбрасывается. Таймаут one-shot 10 мс.

Native STREAM_START: запрос только с id=5 повторяет последние параметры (после boot — оба входа, 10 000 мкс); `<BBI>` задаёт id, mask (1=A6, 2=A7, 3=оба), period_us 1000..10000000. Период относится к каждому выбранному каналу: для mask=3 A6 и A7 измеряются последовательно раз в период, не одновременно. Новый START уже активного native потока получает BUSY. Точный повтор последнего полного START возвращает кэшированный ответ и не перезапускает планировщик.

После одного START Nano автономно запускает измерения по `micros()` и посылает type=3, seq=0, command=`A0`, payload `<BBIIhB>` как у ADC_READ. Каждый отсчёт отдельным сообщением; хост не посылает ADC_READ или keepalive. Native counter растёт на каждый реальный результат, не на dummy, не сбрасывается START/STOP; ADS counter независим. Timestamp — момент обнаружения готовности в loop, не момент апертуры.

Планировщик сохраняет фазу, не выполняет catch-up burst; пропущенные циклы учитываются в adc_missed. Переполнение общей FIFO учитывается в tx_dropped и помечается flags bit1 на следующем успешном образце соответствующего производителя. Native conversion не блокирует обычные UART-команды; READ5, SET_CONFIG5 и новый START5 во время native потока — BUSY. Native STOP не отключает ADS. Оба потока выключены после reset; потеря UART не останавливает поток, аппаратный reset останавливает. Период soft real-time: жёсткий джиттер не гарантируется.

Для обоих ADC sample counter монотонен в пределах включения платы и сбрасывается аппаратным reset; HELLO/START его не сбрасывают. `micros()` оборачивается примерно через 71.6 минуты. Два офлайн scheduler-теста — модель позднего выполнения/занятости/wrap, а не реальный прогон такого переполнения на AVR.

## Хост и безопасность использования

Python-клиент использует стандартную библиотеку и pyserial; API — context manager с явным открытием, ожиданием boot после DTR reset, сопоставлением seq и разделением событий/ответов. Очередь событий ограничена, число host drops доступно клиенту. CLI требует явный порт, не перебирает COM-порты и не посылает диагностические команды ЭБУ. По умолчанию нет записей TPL/DAC и запуска crank. Hardware-тесты не входят в unittest discovery.

Повтор запроса при timeout ограничен одной идентичной wire-посылкой/seq, не является повтором операции с новым seq. COM open сбрасывает Nano на проверенном CH340; программный `--no-reset` не даёт гарантии. При неоднозначном timeout ADC_READ повтор с новым seq может вызвать ещё одно измерение.
