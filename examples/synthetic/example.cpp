extern "C" int forge_example(int n) {
    unsigned sum = 0;
    for (int i = 0; i < n; ++i) sum += static_cast<unsigned>(i);
    return static_cast<int>(sum);
}
