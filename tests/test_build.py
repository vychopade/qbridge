from qbridge.build import parse_diagnostics


def test_compile_errors_and_warnings(fixture_text):
    diags = parse_diagnostics(fixture_text("qcc_compile_errors.txt"))
    errors = [d for d in diags if d.severity == "error"]
    warnings = [d for d in diags if d.severity == "warning"]
    assert len(errors) == 1 and len(warnings) == 3

    e = errors[0]
    assert (e.file, e.line, e.column) == ("broken.c", 9, 5)
    assert e.message == "expected ',' or ';' before 'printf'"

    unused = next(w for w in warnings if w.flag == "-Wunused-variable")
    assert (unused.line, unused.column) == (8, 9)
    assert unused.message == "unused variable 'chid'"


def test_context_lines_are_not_diagnostics(fixture_text):
    diags = parse_diagnostics(fixture_text("qcc_compile_errors.txt"))
    assert all(d.line is not None for d in diags)
    assert not any("In function" in d.message for d in diags)


def test_link_error_names_the_symbol(fixture_text):
    diags = parse_diagnostics(fixture_text("qcc_link_errors.txt"))
    assert len(diags) == 1
    d = diags[0]
    assert (d.kind, d.severity, d.file) == ("link", "error", "linkfail.c")
    assert d.symbol == "not_defined_anywhere"


def test_windows_path_in_diagnostic():
    d = parse_diagnostics(r"C:\work\app\main.c:12:3: error: 'x' undeclared")[0]
    assert (d.file, d.line, d.column) == (r"C:\work\app\main.c", 12, 3)
