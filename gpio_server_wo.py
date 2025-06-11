import subprocess
import time
import threading
from flask import Flask, request
import schedule
import logging
import os
import signal
import atexit

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# Конфигурация
SCHEDULE = {
    7: [("06:00", 15)],  # Физический пин 11: включать в 6:00 на 15 минут Это набор бака
}


PINS = {
    7: 1,
    12: 1
}

gpio_lock = threading.Lock()
scheduler_active = threading.Event()
scheduler_active.set()
active_tasks = {}
task_lock = threading.Lock()

# Функции для работы с GPIO через WiringOP
def gpio_command(pin, value):
    """Выполняет команду gpio с обработкой ошибок"""
    try:
        cmd = f"gpio -1 write {pin} {value}"
        result = subprocess.run(
            cmd, 
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2
        )
        return True
    except subprocess.CalledProcessError as e:
        app.logger.error(f"Ошибка GPIO: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        app.logger.error("Таймаут выполнения GPIO команды")
        return False

def safe_turn_off(pin):
    """Гарантированное выключение пина"""
    for attempt in range(5):
        if gpio_command(pin, 0):
            app.logger.info(f"Пин {pin} ВЫКЛ (гарантированно)")
            return True
        time.sleep(0.5)
    app.logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось выключить пин {pin}!")
    return False

def init_gpio():
    """Инициализация всех пинов"""
    with gpio_lock:
        for pin in PINS.keys():
            # Установка режима вывода
            mode_cmd = f"gpio -1 mode {pin} OUTPUT"
            subprocess.run(mode_cmd, shell=True, check=True)
            # Гарантированное выключение
            safe_turn_off(pin)
            app.logger.info(f"Пин {pin} инициализирован (OUTPUT, LOW)")

# Управление реле
def control_relay(pin, duration):
    try:
        # Регистрация задачи
        with task_lock:
            active_tasks[pin] = {
                'start': time.time(),
                'duration': duration * 60
            }
        
        # Включение
        if not gpio_command(pin, 1):
            app.logger.error(f"Не удалось включить пин {pin}")
            return
            
        app.logger.info(f"Пин {pin} ВКЛ на {duration} мин")
        
        # Ожидание с проверкой каждую секунду
        start_time = time.time()
        end_time = start_time + (duration * 60)
        
        while time.time() < end_time:
            # Проверяем не превысили ли время
            if time.time() - start_time > (duration * 60 * 1.2):
                app.logger.warning(f"Превышение времени работы на пине {pin}!")
                break
            time.sleep(1)  # Проверка каждую секунду
            
    except Exception as e:
        app.logger.error(f"Ошибка управления реле: {str(e)}")
    finally:
        # Выключение в любом случае
        safe_turn_off(pin)
        # Удаление задачи
        with task_lock:
            if pin in active_tasks:
                del active_tasks[pin]

# Мониторинг задач
def task_monitor():
    """Контролирует выполнение активных задач"""
    while scheduler_active.is_set():
        with task_lock:
            current_time = time.time()
            for pin, task in list(active_tasks.items()):
                elapsed = current_time - task['start']
                if elapsed > task['duration'] * 1.5:  # +50% времени
                    app.logger.warning(f"Принудительное выключение пина {pin} (зависшая задача)")
                    threading.Thread(target=safe_turn_off, args=(pin,)).start()
                    del active_tasks[pin]
        time.sleep(5)

# Проверка синхронизации времени
def is_time_synced():
    try:
        result = subprocess.check_output(
            "timedatectl show --property=NTPSynchronized --value",
            shell=True,
            text=True
        )
        return result.strip() == "yes"
    except:
        return False

# Фоновый планировщик
def scheduler_thread():
    app.logger.info("Запуск планировщика...")
    
    # Создание задач
    for pin, tasks in SCHEDULE.items():
        for task_time, duration in tasks:
            schedule.every().day.at(task_time).do(
                lambda p=pin, d=duration: threading.Thread(
                    target=control_relay, args=(p, d), daemon=True
                ).start()
            )
            app.logger.info(f"Запланировано: пин {pin} в {task_time} на {duration} мин")

    while scheduler_active.is_set():
        if is_time_synced():
            schedule.run_pending()
        else:
            app.logger.warning("Время не синхронизировано, пропуск задач...")
            os.system("sudo systemctl restart systemd-timesyncd")
        time.sleep(30)

# Аварийное отключение
def emergency_shutdown():
    app.logger.critical("АВАРИЙНОЕ ОТКЛЮЧЕНИЕ ВСЕХ РЕЛЕ!")
    for pin in SCHEDULE.keys():
        safe_turn_off(pin)
    with task_lock:
        active_tasks.clear()

# Обработчики сигналов
def signal_handler(signum, frame):
    app.logger.warning(f"Получен сигнал {signum}, завершение работы...")
    scheduler_active.clear()
    emergency_shutdown()
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(emergency_shutdown)

# HTTP-эндпоинты
@app.route('/timer')
def handle_timer():
    try:
        duration = int(request.args.get('t', 0))
        pin = int(request.args.get('i', -1))
        
        if duration <= 0 or pin < 0:
            return "Неверные параметры", 400
        
        if pin not in PINS:
            return f"Пин {pin} не настроен", 400
        
        threading.Thread(
            target=control_relay, 
            args=(pin, duration),
            daemon=True
        ).start()
        
        return f"Реле на пине {pin} активировано на {duration} мин."
    
    except Exception as e:
        return f"Ошибка: {str(e)}", 500

@app.route('/emergency_stop')
def emergency_stop():
    emergency_shutdown()
    return "АВАРИЙНОЕ ОТКЛЮЧЕНИЕ ВЫПОЛНЕНО"

if __name__ == '__main__':
    # Проверка наличия gpio
    try:
        subprocess.run("gpio -v", shell=True, check=True, stdout=subprocess.DEVNULL)
    except:
        app.logger.critical("Команда 'gpio' не найдена! Установите WiringOP.")
        exit(1)
    
    init_gpio()
    
    # Дополнительная проверка: все ли пины выключены
    for pin in PINS.keys():
        # Проверяем состояние пина
        try:
            result = subprocess.run(
                f"gpio -1 read {pin}",
                shell=True,
                capture_output=True,
                text=True
            )
            if result.stdout.strip() == "1":
                app.logger.warning(f"Пин {pin} был ВКЛ при старте! Выключаем...")
                safe_turn_off(pin)
        except:
            pass
    
    # Запуск фоновых служб
    threading.Thread(target=scheduler_thread, daemon=True).start()
    threading.Thread(target=task_monitor, daemon=True).start()
    
    app.logger.info("Сервер запущен")
    app.run(host='0.0.0.0', port=80, threaded=True)
