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

local function google_search_link(python, file, expr, line, callback)
  vim.system(
    { python, helper_script(), "--search-link", file, expr, tostring(line) },
    { text = true },
    function(result)
      local link = vim.trim(result.stdout or "")
      vim.schedule(function() callback(result.code == 0 and link ~= "" and link or nil) end)
    end
  )
end

local function show(lines, opts, on_search_click)
  -- First call opens unfocused (`nvim_open_win` enter=false). A later call with
  -- the same focus_id enters that window, matching vim.lsp.buf.hover. Do not
  -- set close_events: the default CursorMoved dismissal must stay.
  local bufnr, winid = vim.lsp.util.open_floating_preview(lines, "markdown", {
    border = opts.border,
    wrap = true,
    focus = true,
    focus_id = "python_runtime_doc",
    max_width = math.min(opts.max_width, vim.o.columns - 8),
    max_height = math.min(opts.max_height, vim.o.lines - 6),
  })
  if on_search_click and bufnr and winid then
    vim.keymap.set("n", "<LeftMouse>", function()
      local mouse = vim.fn.getmousepos()
      if mouse.winid ~= winid or mouse.line <= 0 then
        return
      end
      local ok, buf_lines = pcall(vim.api.nvim_buf_get_lines, bufnr, mouse.line - 1, mouse.line, false)
      local buf_line = (ok and buf_lines[1]) or ""
      if buf_line:find("https://www%.google%.com/search", 1) then
        on_search_click()
        return
      end
      pcall(vim.api.nvim_win_set_cursor, winid, { mouse.line, math.max((mouse.column or 1) - 1, 0) })
    end, { buffer = bufnr, silent = true, desc = "Open API documentation search" })
  end
end

local function current_context()
  local expr = expression_under_cursor()
  if not expr then
    vim.notify("No name under cursor", vim.log.levels.WARN)
    return nil
  end
  local file = vim.api.nvim_buf_get_name(0)
  local line = vim.api.nvim_win_get_cursor(0)[1]
  return expr, file, line
end

function M.lookup(opts)
  opts = vim.tbl_deep_extend("force", defaults, opts or M.config or {})
  local expr, file, line = current_context()
  if not expr then return end
  local python = project_python(file, opts)
  if python == "" then
    vim.notify("python3 executable not found", vim.log.levels.ERROR)
    return
  end

  vim.system({ python, helper_script(), file, expr, tostring(line) }, { text = true }, function(result)
    local text = (result.stdout or "") .. (result.stderr or "")
    if vim.trim(text) == "" then text = ("(No output; exit code %d)"):format(result.code) end
    google_search_link(python, file, expr, line, function(link)
      vim.schedule(function()
        local lines = vim.split(vim.trim(text, "\n"), "\n", { plain = true })
        if link then
          table.insert(lines, "")
          table.insert(lines, link)
          local url = link:match("%((https://www%.google%.com/search%?q=[^%)]+)%)$")
          show(lines, opts, url and function()
            local ok, err = pcall(vim.ui.open, url)
            if not ok then vim.notify("Could not open Google search: " .. tostring(err), vim.log.levels.ERROR) end
          end or nil)
        else
          show(lines, opts)
        end
      end)
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
      if not vim.tbl_contains(M.config.filetypes, vim.bo[bufnr].filetype) then return end
      if M.config.keymap and M.config.keymap ~= "" then
        vim.keymap.set("n", M.config.keymap, function() M.lookup(M.config) end, {
          buffer = bufnr,
          desc = "Python runtime documentation",
          silent = true,
        })
      end
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
