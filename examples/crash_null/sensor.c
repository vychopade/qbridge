/*
 * Demo app with a planted bug: a null pointer dereference.
 *
 * A "sensor reader" looks up a calibration entry by sensor id. Lookup returns
 * NULL for unknown ids, and the caller forgets to check. Sensor 7 is unknown,
 * so the process dies with SIGSEGV and dumper writes a core file.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

struct calibration {
    int sensor_id;
    double offset;
    double scale;
};

static struct calibration table[] = {
    { 1, 0.5, 1.02 },
    { 2, -0.1, 0.98 },
    { 3, 0.0, 1.00 },
};

static struct calibration *find_calibration(int sensor_id)
{
    for (size_t i = 0; i < sizeof(table) / sizeof(table[0]); i++) {
        if (table[i].sensor_id == sensor_id) {
            return &table[i];
        }
    }
    return NULL;
}

static double apply_calibration(int sensor_id, double raw)
{
    struct calibration *cal = find_calibration(sensor_id);
    /* BUG: cal is NULL for unknown sensors. */
    return (raw + cal->offset) * cal->scale;
}

int main(int argc, char **argv)
{
    int sensors[] = { 1, 2, 3, 7 };
    double raw = argc > 1 ? atof(argv[1]) : 10.0;

    for (size_t i = 0; i < sizeof(sensors) / sizeof(sensors[0]); i++) {
        double value = apply_calibration(sensors[i], raw);
        printf("sensor %d: %.3f\n", sensors[i], value);
        fflush(stdout);
    }
    return 0;
}
