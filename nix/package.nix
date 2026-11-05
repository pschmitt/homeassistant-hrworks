{
  lib,
  python3Packages,
  installShellFiles,
  browser-daemon-client,
}:
python3Packages.buildPythonApplication {
  pname = "hrworks-employee";
  version = "9.2.0";
  pyproject = true;
  src = lib.cleanSource ../.;
  build-system = [ python3Packages.hatchling ];
  dependencies = [
    browser-daemon-client
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
  pythonRelaxDeps = [ "browser-daemon-client" ];
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export PYTHONPATH="$out/${python3Packages.python.sitePackages}:$PWD:$PYTHONPATH"
    python -m unittest discover -s tests -p test_cache.py
    python -m unittest discover -s tests -p test_cli.py
    python -m unittest discover -s tests -p test_totp.py
    runHook postCheck
  '';
  pythonImportsCheck = [ "hrworks" ];
  meta = {
    description = "HR WORKS employee library and pretty TSV CLI on browser-daemon";
    homepage = "https://github.com/pschmitt/homeassistant-hrworks";
    license = lib.licenses.mit;
    mainProgram = "hrworks";
    platforms = lib.platforms.linux;
  };
}
