from qbridge.parsers import pidin


def _threads(fixture_text):
    return pidin.parse_threads(fixture_text("pidin_threads.txt"))


def test_parses_every_thread_line(fixture_text):
    text = fixture_text("pidin_threads.txt")
    data_lines = [l for l in text.splitlines()[1:] if l.strip()]
    assert len(_threads(fixture_text)) == len(data_lines) == 111


def test_kernel_idle_thread(fixture_text):
    t = _threads(fixture_text)[0]
    assert (t.pid, t.tid) == (1, 1)
    assert t.name == "procnto-smp-instr"
    assert (t.priority, t.policy) == (0, "FIFO")
    assert t.state == "READY"
    assert t.thread_name == "idle_cpu_0"
    assert t.blocked_on_pid is None


def test_interrupt_and_round_robin_priorities(fixture_text):
    ts = _threads(fixture_text)
    ipi = next(t for t in ts if t.thread_name == "ipi_cpu_0")
    assert (ipi.priority, ipi.policy, ipi.state) == (255, "INTERRUPT", "INTR")
    slog = next(t for t in ts if t.name == "slogger2")
    assert (slog.priority, slog.policy) == (10, "RR")


def test_reply_blocked_points_at_server_pid(fixture_text):
    ts = _threads(fixture_text)
    ksh = next(t for t in ts if t.pid == 57351)
    assert ksh.state == "REPLY"
    assert ksh.blocked_on_pid == 49157


def test_receive_and_sem_are_not_pids(fixture_text):
    ts = _threads(fixture_text)
    for t in ts:
        if t.state in ("RECEIVE", "SEM", "CONDVAR"):
            assert t.blocked_on_pid is None
    sem = next(t for t in ts if t.state == "SEM")
    assert sem.blocked  # raw address kept


def test_blocked_graph_names_the_server(fixture_text):
    edges = pidin.blocked_graph(_threads(fixture_text))
    ksh_edges = [e for e in edges if e["from_name"] == "ksh"]
    assert len(ksh_edges) == 4
    assert all(e["to_name"] == "devc-con" for e in ksh_edges)
    assert pidin.find_cycles(edges) == []


def test_processes_groups_threads(fixture_text):
    procs = pidin.processes(_threads(fixture_text))
    kernel = procs[0]
    assert kernel["pid"] == 1 and len(kernel["threads"]) == 16


def test_send_deadlock_cycle_detected():
    text = (
        "     pid|tid|name|prio|STATE|Blocked|thread name\n"
        "     100|  1|/tmp/a| 10r|SEND          |200       |main\n"
        "     200|  1|/tmp/b| 10r|SEND          |100       |main\n"
        "     300|  1|/tmp/c| 10r|REPLY         |100       |main\n"
    )
    edges = pidin.blocked_graph(pidin.parse_threads(text))
    assert len(edges) == 3
    assert pidin.find_cycles(edges) == [[100, 200]]
