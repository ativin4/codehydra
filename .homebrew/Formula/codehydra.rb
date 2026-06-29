class Codehydra < Formula
  include Language::Python::Virtualenv

  desc "BYOS multi-CLI coding agent TUI — route prompts across Claude, Gemini, Codex and Ollama"
  homepage "https://github.com/ativin4/codehydra"
  url "https://files.pythonhosted.org/packages/source/c/codehydra/codehydra-0.1.0.tar.gz"
  # Update sha256 after first PyPI publish: `shasum -a 256 codehydra-0.1.0.tar.gz`
  sha256 "PLACEHOLDER_UPDATE_AFTER_PYPI_PUBLISH"
  license "MIT"

  depends_on "python@3.12"

  resource "google-auth" do
    url "https://files.pythonhosted.org/packages/source/g/google-auth/google_auth-2.53.0.tar.gz"
    sha256 "" # fill after `brew fetch --build-from-source`
  end

  resource "mcp" do
    url "https://files.pythonhosted.org/packages/source/m/mcp/mcp-1.27.2.tar.gz"
    sha256 ""
  end

  resource "pathspec" do
    url "https://files.pythonhosted.org/packages/source/p/pathspec/pathspec-1.1.1.tar.gz"
    sha256 ""
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-15.0.0.tar.gz"
    sha256 ""
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/source/t/textual/textual-8.2.7.tar.gz"
    sha256 ""
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/source/t/typer/typer-0.26.7.tar.gz"
    sha256 ""
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "codehydra", shell_output("#{bin}/codehydra --help")
  end
end
