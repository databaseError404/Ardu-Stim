# Python host — заметки

Клиент находится в `host/bench_client.py`, CLI — `host/bench_cli.py`. Импорт не открывает serial-порт. Используются Python standard library и `pyserial` (`requirements.txt`). `BenchClient` поддерживает context manager, явное открытие, ожидание boot после reset, случайный ненулевой seq, проверку соответствия ответа и отдельный bounded event queue с `event_drops`.

## Событийный поток

`nano-stream` посылает один `ADC_STREAM_START` с native mask/period, затем читает UART-события и в `finally` отправляет STOP. Получение отсчётов не вызывает `ADC_READ`; каждый результат приходит отдельным событием. Опции: `--period-us` (1 000..10 000 000), `--channels 6 7` (по умолчанию оба), `--seconds` и/или `--count`. `--count` работает с ожиданием UART, а не только с неблокирующей проверкой очереди. Вывод каждой записи — JSONL с `host_monotonic`, объектом sample и `host_event_drops`. Одновременные ADS события различаются по `sample.id`.

Код Python:

```python
with BenchClient("COM79") as stand:
    stand.nano_adc_stream_start(period_us=10_000, channels=(6, 7))
    try:
        for sample in stand.iter_events(duration=5):
            print(sample)
    finally:
        stand.nano_adc_stream_stop()
```

CLI требует явный `--port`, default baud 1 000 000. Набор команд включает hello/status/devices/scan/config/adc-read/nano-read/nano-stream/adc-stream/set-output/get-output/crank-config/rpm/stop/adc-stop. `nano-read --channel 6|7` конфигурирует native one-shot channel и читает один sample. По умолчанию CLI не записывает TPL/MCP и не запускает crank; аппаратные тесты не входят в unittest discovery. Нет авто-перебора COM и передачи диагностических команд ЭБУ.

## Таймауты, retry и reset

Обычный request timeout — 1 с, scan — 2 с. По умолчанию допускается не более одного retry, и только идентичного wire-кадра с тем же seq. Серверный cache хранит лишь последний запрос и ответ. Поэтому точный повтор команды из кэша не повторяет операцию; это не гарантия дедупликации после того, как кэш заменён более поздним запросом. При неоднозначном таймауте ADC_READ клиент не должен автоматически создавать новый seq: это может инициировать дополнительное физическое измерение.

Открытие COM на проверенном CH340 аппаратно сбрасывает Nano. Опция `--no-reset` пытается снять DTR/RTS до открытия, но драйвер может кратко установить сигнал; это не предотвратило reset на данном адаптере. Для гарантии между переподключениями требуется аппаратно отключить автосброс. Для последовательности команд используйте одно соединение. Историческое завершение испытаний с потоками OFF и COM free описывает состояние в тот момент и не свидетельствует о текущем состоянии стенда.

Установка зависимости: `python -m pip install -r requirements.txt`. Исторические 26 offline тестов перечислены в [`HARDWARE_REPORT.md`](HARDWARE_REPORT.md); новые тесты могут изменить их число. Scheduler model-тесты не являются исполнением прошивки на AVR.
