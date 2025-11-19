def format_days_hours(value: float) -> str:
    """
    Конвертирует количество дней (float) → строка вида:
    '3 дня 5 часов', '1 день 2 часа', '5 часов', '0 часов'
    """
    if value <= 0:
        return "0 часов"

    days = int(value)
    hours = int(round((value - days) * 24))

    # корректируем случаи типа 23.9 → 24 часов
    if hours == 24:
        days += 1
        hours = 0

    parts = []

    if days > 0:
        # правильное склонение
        if days % 10 == 1 and days % 100 != 11:
            parts.append(f"{days} день")
        elif 2 <= days % 10 <= 4 and not (12 <= days % 100 <= 14):
            parts.append(f"{days} дня")
        else:
            parts.append(f"{days} дней")

    if hours > 0:
        # склонение для часов
        if hours % 10 == 1 and hours % 100 != 11:
            parts.append(f"{hours} час")
        elif 2 <= hours % 10 <= 4 and not (12 <= hours % 100 <= 14):
            parts.append(f"{hours} часа")
        else:
            parts.append(f"{hours} часов")

    return " ".join(parts)