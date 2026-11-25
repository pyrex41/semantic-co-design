"""Run the five-chapter SICP specialization checks."""

from . import check_languages, check_procedures_data, check_state


def check():
    for label, value in check_procedures_data.check().items():
        print(f"Chapters 1–2 {label}: {value}")
    check_state.check()
    for label, value in check_languages.check().items():
        print(f"Chapters 4–5 {label}: {value}")


if __name__ == "__main__":
    check()
