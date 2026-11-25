{
  description = "HR WORKS employee CLI and Python library (browser-daemon), and the Home Assistant integration";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    browser-daemon = {
      url = "github:pschmitt/browser-daemon";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      browser-daemon,
    }:
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
          hrworks = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix {
            inherit (browser-daemon.packages.${system}) browser-daemon-client;
          };
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
        default = self.nixosModules.cli;
        cli = import ./nix/cli-module.nix self;
      };
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          # Home Assistant's own Python package set, so the integration tests (tests/ha) run
          # in the shell too, against the Home Assistant release nixpkgs ships. Everything
          # else is built from the same set: one interpreter, no colliding copies.
          haPython = pkgs.home-assistant.python3Packages;
          browser-daemon-client = haPython.callPackage "${browser-daemon}/nix/python-client.nix" { };
          python = haPython.python.withPackages (ps: [
            ps.aiohttp
            ps.probatio
            ps.rich
            ps.typer
            (ps.toPythonModule pkgs.home-assistant)
            browser-daemon-client
            # Installed distribution metadata for the CLI version lookup; the
            # sources on PYTHONPATH (see shellHook) take precedence.
            (ps.toPythonModule (
              self.packages.${system}.hrworks.override {
                python3Packages = haPython;
                inherit browser-daemon-client;
              }
            ))
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.just
              pkgs.nixfmt
              pkgs.nodejs
              pkgs.ruff
            ];
            # The tests import the repo root (custom_components) and hrworks from
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
