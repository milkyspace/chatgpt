import os
import glob


def simple_collect_code(output_file="project_code.txt"):
    """
    Простая версия для сбора Python файлов
    """
    # Ищем все .py файлы в текущей директории и поддиректориях
    py_files = glob.glob('**/*.py', recursive=True)

    # Исключаем системные директории
    exclude_dirs = ['__pycache__', '.git', 'venv', 'env']
    filtered_files = [f for f in py_files if not any(exclude in f for exclude in exclude_dirs)]

    # Сортируем по имени
    filtered_files.sort()

    with open(output_file, 'w', encoding='utf-8') as out_file:
        for file_path in filtered_files:
            try:
                with open(file_path, 'r', encoding='utf-8') as in_file:
                    content = in_file.read()

                out_file.write(f"{file_path}:\n")
                out_file.write("=" * 40 + "\n")
                out_file.write(content)
                out_file.write("\n\n" + "=" * 60 + "\n\n")

                print(f"Добавлен: {file_path}")

            except Exception as e:
                print(f"Ошибка с файлом {file_path}: {e}")

    print(f"\nГотово! Файлы сохранены в {output_file}")


# Запуск
if __name__ == "__main__":
    simple_collect_code()