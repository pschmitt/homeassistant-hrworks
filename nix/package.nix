{
  lib,
  python3Packages,
  installShellFiles,
}:
python3Packages.buildPythonApplication {
  pname = "hrworks-employee";
  version = "2.0.1";
  pyproject = true;
  src = lib.cleanSource ../.;
  build-system = [ python3Packages.hatchling ];
  dependencies = [
    python3Packages.asyncssh
    python3Packages.playwright
    python3Packages.rich
    python3Packages.typer
  ];
  nativeBuildInputs = [ installShellFiles ];
  postInstall = ''
    installShellCompletion --cmd hrworks \
      --bash <($out/bin/hrworks completion bash) \
      --zsh <($out/bin/hrworks completion zsh) \
      --fish <($out/bin/hrworks completion fish)
  '';
  # Nix supplies the dependency version; the lock pins nixpkgs and its Python closure.
  pythonRelaxDeps = [ "playwright" ];
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export PYTHONPATH="$out/${python3Packages.python.sitePackages}:$PWD:$PYTHONPATH"
    python -m unittest discover -s tests -p test_cli.py
    python -m unittest discover -s tests -p test_transport.py
    python -m unittest discover -s tests -p test_totp.py
    python -m unittest discover -s tests -p test_record.py
    runHook postCheck
  '';
  pythonImportsCheck = [
    "hrworks"
    "hrworks_worker"
  ];
  meta = {
    description = "HR WORKS employee library, pretty TSV CLI and browser worker";
    homepage = "https://github.com/pschmitt/homeassistant-hrworks";
    license = lib.licenses.mit;
    mainProgram = "hrworks";
    platforms = lib.platforms.linux;
  };
}
