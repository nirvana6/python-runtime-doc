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

local function helper_script()
  local source = debug.getinfo(1, "S").source:sub(2)
  local module_dir = vim.fs.dirname(source)
  local lua_dir = vim.fs.dirname(module_dir)
  local plugin_root = vim.fs.dirname(lua_dir)
  return plugin_root .. "/python/runtime_doc.py"
end

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
  local line = vim.api.nvim_win_get_cursor(0)[1]
  local python = project_python(file, opts)
  if python == "" then
    vim.notify("python3 executable not found", vim.log.levels.ERROR)
    return
  end

  vim.system({ python, helper_script(), file, expr, tostring(line) }, { text = true }, function(result)
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
      local line = vim.api.nvim_win_get_cursor(0)[1]
      local python = project_python(file, M.config)
      vim.system({ python, helper_script(), file, args.args, tostring(line) }, { text = true }, function(result)
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
