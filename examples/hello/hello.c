/* Smallest possible QNX program, used to check build -> deploy -> run. */
#include <stdio.h>
#include <sys/neutrino.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    printf("hello from QNX: pid %d, running on cpu %d\n", getpid(), SchedGetCpuNum());
    for (int i = 1; i < argc; i++) {
        printf("arg %d: %s\n", i, argv[i]);
    }
    return 0;
}
