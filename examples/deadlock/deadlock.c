/*
 * Demo app with a planted bug: a deadlock over QNX message passing.
 *
 * Two processes, each with its own channel:
 *   config  (parent) keeps the configuration and answers "get" requests
 *   sensor  (child)  serves readings, and needs the calibration from config
 *
 * At start-up, config asks sensor for its first reading (MsgSend: config is
 * now REPLY-blocked on sensor). To compute the reading, sensor asks config
 * for the calibration (MsgSend to config). But config's only thread is still
 * waiting for sensor's reply, so nobody is receiving on config's channel:
 * sensor is SEND-blocked on config, config is REPLY-blocked on sensor.
 * Neither will ever move again.
 *
 * Usage: deadlock
 */
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/neutrino.h>
#include <unistd.h>

enum { GET_READING = 1, GET_CALIBRATION = 2 };

struct msg {
    int    type;
    double value;
};

static void run_sensor(pid_t config_pid, int config_chid, int pipe_fd)
{
    pthread_setname_np(pthread_self(), "sensor-main");
    int chid = ChannelCreate(0);
    if (chid == -1) {
        perror("sensor: ChannelCreate");
        exit(EXIT_FAILURE);
    }
    /* Tell config which channel to use. */
    if (write(pipe_fd, &chid, sizeof(chid)) != sizeof(chid)) {
        exit(EXIT_FAILURE);
    }
    close(pipe_fd);

    int config_coid = ConnectAttach(0, config_pid, config_chid, _NTO_SIDE_CHANNEL, 0);
    if (config_coid == -1) {
        perror("sensor: ConnectAttach");
        exit(EXIT_FAILURE);
    }

    for (;;) {
        struct msg in;
        int rcvid = MsgReceive(chid, &in, sizeof(in), NULL);
        if (rcvid <= 0) {
            continue;
        }
        if (in.type == GET_READING) {
            /* BUG: calls back into config while config is still waiting for
             * this reply. config is single-threaded and REPLY-blocked on us,
             * so this MsgSend never gets received. */
            struct msg ask = { GET_CALIBRATION, 0 }, cal;
            if (MsgSend(config_coid, &ask, sizeof(ask), &cal, sizeof(cal)) == -1) {
                perror("sensor: MsgSend to config");
                exit(EXIT_FAILURE);
            }
            struct msg out = { GET_READING, 21.5 * cal.value };
            MsgReply(rcvid, EOK, &out, sizeof(out));
        }
    }
}

int main(void)
{
    pthread_setname_np(pthread_self(), "config-main");
    int chid = ChannelCreate(0);
    if (chid == -1) {
        perror("config: ChannelCreate");
        return EXIT_FAILURE;
    }
    int fds[2];
    if (pipe(fds) == -1) {
        perror("pipe");
        return EXIT_FAILURE;
    }

    pid_t config_pid = getpid();
    pid_t sensor = fork();
    if (sensor == 0) {
        close(fds[0]);
        run_sensor(config_pid, chid, fds[1]);
        return EXIT_SUCCESS;
    }
    close(fds[1]);
    int sensor_chid;
    if (read(fds[0], &sensor_chid, sizeof(sensor_chid)) != sizeof(sensor_chid)) {
        fprintf(stderr, "config: sensor did not start\n");
        return EXIT_FAILURE;
    }
    int sensor_coid = ConnectAttach(0, sensor, sensor_chid, _NTO_SIDE_CHANNEL, 0);
    if (sensor_coid == -1) {
        perror("config: ConnectAttach");
        return EXIT_FAILURE;
    }
    printf("config %d, sensor %d: asking sensor for its first reading\n", config_pid, sensor);
    fflush(stdout);

    /* Ask for the first reading before entering the receive loop. */
    struct msg ask = { GET_READING, 0 }, reading;
    if (MsgSend(sensor_coid, &ask, sizeof(ask), &reading, sizeof(reading)) == -1) {
        perror("config: MsgSend to sensor");
        kill(sensor, SIGTERM);
        return EXIT_FAILURE;
    }
    printf("first reading: %.2f\n", reading.value);
    fflush(stdout);

    /* Normal service loop (never reached while the bug is there). */
    for (;;) {
        struct msg in;
        int rcvid = MsgReceive(chid, &in, sizeof(in), NULL);
        if (rcvid <= 0) {
            continue;
        }
        struct msg out = { in.type, 1.0 };
        MsgReply(rcvid, EOK, &out, sizeof(out));
    }
}
