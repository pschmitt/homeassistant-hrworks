{
  lib,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "hrworks-browser-worker";
  version = "1.0.0";
  pyproject = true;
  src = lib.cleanSource ../.;
  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.playwright ];
  # Nix supplies the dependency version; the lock pins nixpkgs and its Python closure.
  pythonRelaxDeps = [ "playwright" ];
  doCheck = false;
  meta = {
    description = "HR WORKS employee browser process accessed over SSH";
    homepage = "https://github.com/pschmitt/homeassistant-hrworks";
    license = lib.licenses.mit;
    mainProgram = "hrworks-worker";
    platforms = lib.platforms.linux;
  };
}
