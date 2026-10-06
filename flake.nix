{
  description = "HR WORKS employee CLI, Python library and Home Assistant browser worker";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      packages = forAllSystems (
        system:
        let
          hrworks = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix { };
        in
        {
          inherit hrworks;
          default = hrworks;
        }
      );
      apps = forAllSystems (system: {
        default = self.apps.${system}.hrworks;
        hrworks = {
          type = "app";
          program = "${self.packages.${system}.hrworks}/bin/hrworks";
        };
      });
      nixosModules = {
        default = self.nixosModules.worker;
        worker = import ./nix/module.nix self;
        cli = import ./nix/cli-module.nix self;
      };
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (
            ps: with ps; [
              asyncssh
              playwright
              rich
              typer
              # Installed distribution metadata for the CLI version lookup; the
              # sources on PYTHONPATH (see shellHook) take precedence.
              (toPythonModule self.packages.${system}.hrworks)
            ]
          );
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.just
              pkgs.nixfmt
              pkgs.ruff
            ];
            # Browser tests need a Chromium that matches nixpkgs' Playwright.
            PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
            PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
            # The tests import hrworks_worker from the repo root and hrworks from
            # the component directory, as the wheel layout does. Link only the
            # hrworks package: putting custom_components/hrworks itself on the
            # path would let its calendar.py shadow the standard library.
            shellHook = ''
              dev_path="''${TMPDIR:-/tmp}/hrworks-dev-pythonpath-$(id -u)"
              mkdir -p "$dev_path"
              ln -sfn "$PWD/custom_components/hrworks/hrworks" "$dev_path/hrworks"
              export PYTHONPATH="$PWD:$dev_path''${PYTHONPATH:+:$PYTHONPATH}"
            '';
          };
        }
      );
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
