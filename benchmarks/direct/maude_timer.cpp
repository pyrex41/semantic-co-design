// Input/output adapter for maudec's generated C++ algebraic data types.
#include KERNEL
#include <cstdint>
#include <ctime>
#include <iostream>
#include <vector>

static Row empty() { return Row(std::in_place_index<Row::NIL>); }

static Row run(const Row& row) {
#if defined(BENCH_VM)
    return checksum(row, 0, 17);
#elif defined(BENCH_QUERY)
    return query(row);
#elif defined(BENCH_RAILWAY)
    return rail(row, 0, 0, 0);
#else
    return chunks(row, State(std::in_place_index<State::ST>, 0,-1,0,0,0,0,0,empty(),empty()));
#endif
}
static double cpu() {
    timespec t;
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t)) std::exit(2);
    return t.tv_sec + 1e-9 * t.tv_nsec;
}
int main() {
    size_t count;
    if (!(std::cin >> count) || count == 0 || count > 4096) return 2;
    std::vector<Row> rows, answers;
    answers.reserve(count);
    for (size_t i = 0; i < count; ++i) {
        size_t n, capacity;
        if (!(std::cin >> n >> capacity) || n > 65536) return 2;
        std::vector<int> values(n);
        for (auto& x : values) if (!(std::cin >> x)) return 2;
        Row row = empty();
        for (auto j = values.rbegin(); j != values.rend(); ++j)
            row = Row(std::in_place_index<Row::CONS>, *j, new Row(std::move(row)));
        rows.push_back(std::move(row));
    }
    auto warm = run(rows.front());
    double start = cpu();
    for (const auto& row : rows) answers.push_back(run(row));
    std::cout.precision(12);
    std::cout << cpu() - start << '\n';
    for (const auto& answer : answers) {
        const Row* p = &answer;
#ifdef BENCH_VM
        uint64_t word = 0;
#endif
        while (p->index() == Row::CONS) {
            const auto& [v, tail] = std::get<Row::CONS>(*p);
#ifdef BENCH_VM
            word = word * 65536 + v;
#else
            std::cout << v << ' ';
#endif
            p = tail.get();
        }
        if (p->index() != Row::NIL) return 2;
#ifdef BENCH_VM
        std::cout << word;
#endif
        std::cout << '\n';
    }
}
