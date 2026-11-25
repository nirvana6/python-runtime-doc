local M = {}

local defaults = {
  keymap = "<leader>K",
  filetypes = { "python" },
  python = nil,
  max_width = 100,
  max_height = 24,
  border = "rounded",
  focus = false,
}

local SCRIPT = [==[
import ast, builtins, importlib, inspect, sys

path, expr = sys.argv[1], sys.argv[2]
expr = expr.strip().strip(".")


def imported_modules(path):
    mods = []
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except Exception:
        return mods
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.append(node.module)
    return list(dict.fromkeys(mods))


def resolve(expr):
    parts = expr.split(".")
    if len(parts) > 1:
        for i in range(len(parts), 0, -1):
            try:
                obj = importlib.import_module(".".join(parts[:i]))
            except Exception:
                continue
            for part in parts[i:]:
                obj = getattr(obj, part)
            return expr, obj
        raise ImportError(parts[0])

    name = parts[0]
    for module_name in imported_modules(path):
        candidates = dict.fromkeys((module_name, module_name.split(".")[0]))
        for candidate in candidates:
            try:
                module = importlib.import_module(candidate)
            except Exception:
                continue
            if hasattr(module, name):
                return f"{candidate}.{name}", getattr(module, name)
    if hasattr(builtins, name):
        return name, getattr(builtins, name)
    raise LookupError(name)


try:
    title, obj = resolve(expr)
except Exception as exc:
    print(f"Cannot resolve {expr!r}: {type(exc).__name__}: {exc}")
    print()
    print("Tip: place the cursor on a qualified expression such as mujoco.mj_step.")
    print("Bare names are resolved by searching imports in the current file.")
    sys.exit(1)

kind = type(obj).__name__
print(f"{title}    <{kind}>")
print("-" * max(20, min(len(title) + len(kind) + 8, 78)))
print()
doc = inspect.getdoc(obj)
if doc:
    print(doc)
else:
    try:
        print(f"signature: {inspect.signature(obj)}")
    except Exception:
        pass
    print("(No docstring available.)")
]==]

local function project_python(file, opts)
  if opts.python and opts.python ~= "" then return opts.python end

  local dir = vim.fs.dirname(file or "")
  if not dir or dir == "" then dir = vim.uv.cwd() end
  for _ = 1, 10 do
    local candidate = dir .. "/.venv/bin/python"
    if vim.fn.executable(candidate) == 1 then return candidate end
    local parent = vim.fs.dirname(dir)
    if not parent or parent == dir then break end
    dir = parent
  end

  local venv = vim.env.VIRTUAL_ENV
  if venv and venv ~= "" and vim.fn.executable(venv .. "/bin/python") == 1 then
    return venv .. "/bin/python"
  end
  return vim.fn.exepath("python3")
end

local function expression_under_cursor()
  local line = vim.api.nvim_get_current_line()
  local col = vim.api.nvim_win_get_cursor(0)[2] + 1
  local init = 1
  while true do
    local start_col, end_col = line:find("[%w_%.]+", init)
    if not start_col then break end
    if col >= start_col and col <= end_col then
      local token = line:sub(start_col, end_col):gsub("^%.+", ""):gsub("%.+$", "")
      if token ~= "" then return token end
      break
    end
    init = end_col + 1
  end
  local word = vim.fn.expand("<cword>")
  return word ~= "" and word or nil
end

local function show(lines, opts)
  vim.lsp.util.open_floating_preview(lines, "plaintext", {
    border = opts.border,
    wrap = true,
    focus = opts.focus,
    max_width = math.min(opts.max_width, vim.o.columns - 8),
    max_height = math.min(opts.max_height, vim.o.lines - 6),
  })
end

function M.lookup(opts)
  opts = vim.tbl_deep_extend("force", defaults, opts or M.config or {})
  local expr = expression_under_cursor()
  if not expr then
    vim.notify("No name under cursor", vim.log.levels.WARN)
    return
  end

  local file = vim.api.nvim_buf_get_name(0)
  local python = project_python(file, opts)
  if python == "" then
    vim.notify("python3 executable not found", vim.log.levels.ERROR)
    return
  end

  vim.system({ python, "-c", SCRIPT, file, expr }, { text = true }, function(result)
    local text = (result.stdout or "") .. (result.stderr or "")
    if vim.trim(text) == "" then text = ("(No output; exit code %d)"):format(result.code) end
    vim.schedule(function()
      show(vim.split(vim.trim(text, "\n"), "\n", { plain = true }), opts)
    end)
  end)
end

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", defaults, opts or {})

  vim.api.nvim_create_user_command("PythonRuntimeDoc", function(args)
    if args.args ~= "" then
      local file = vim.api.nvim_buf_get_name(0)
      local python = project_python(file, M.config)
      vim.system({ python, "-c", SCRIPT, file, args.args }, { text = true }, function(result)
        local text = (result.stdout or "") .. (result.stderr or "")
        if vim.trim(text) == "" then text = ("(No output; exit code %d)"):format(result.code) end
        vim.schedule(function()
          show(vim.split(vim.trim(text, "\n"), "\n", { plain = true }), M.config)
        end)
      end)
    else
      M.lookup(M.config)
    end
  end, { nargs = "?", desc = "Show Python runtime documentation", force = true })

  if M.config.filetypes and #M.config.filetypes > 0 then
    local function attach(bufnr)
      if not M.config.keymap or M.config.keymap == "" then return end
      if not vim.tbl_contains(M.config.filetypes, vim.bo[bufnr].filetype) then return end
      vim.keymap.set("n", M.config.keymap, function() M.lookup(M.config) end, {
        buffer = bufnr,
        desc = "Python runtime documentation",
        silent = true,
      })
    end

    local group = vim.api.nvim_create_augroup("PythonRuntimeDoc", { clear = true })
    vim.api.nvim_create_autocmd("FileType", {
      group = group,
      pattern = M.config.filetypes,
      callback = function(args) attach(args.buf) end,
    })
    attach(0)
  end
end

return M
