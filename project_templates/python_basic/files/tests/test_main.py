import main


def test_main(capsys):
    main.main()
    assert "{{name}}" in capsys.readouterr().out
