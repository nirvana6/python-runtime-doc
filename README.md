# python-runtime-doc

Neovim 中的 Python 运行时文档查询插件。把光标放在函数或对象上，调用项目 Python 解释器读取它的运行时文档，并在浮窗中查看签名与用途。

它特别适用于没有类型存根（`.pyi` / `py.typed`）的编译型 Python 扩展：静态语言服务器可能只能显示 `Unknown`，但扩展对象的运行时 `__doc__` 仍可能包含签名和说明。

## 功能

- 在 Neovim 浮窗中显示 Python 对象的运行时文档。
- 优先沿当前文件路径向上查找 `.venv/bin/python`，其次使用 `$VIRTUAL_ENV/bin/python`，最后使用 `python3`。
- 支持光标下的点号表达式：既包括 `mujoco.mj_step`，也包括 `history_positions.append` 这样的对象方法。
- 对象方法先按当前文件源码中的简单赋值或类型注解推断接收对象类型，再查询该类型的方法文档；例如 `history_positions = []` 可推断为 `list`。
- 裸名称可根据当前文件的 `import` 语句或可识别的赋值尝试解析。
- 类型无法可靠推断时显示原因，不执行当前 Python 文件，也不猜测对象类型。
- 可配置快捷键、Python 解释器路径、浮窗尺寸和文件类型。
- 不依赖 AstroNvim；可用于普通 Neovim 配置。

## 环境要求

- Neovim 0.10 或更新版本（使用 `vim.system`）。
- Python 3。
- 目标库必须能被所选 Python 解释器导入。

插件不安装 Python 包，也不创建虚拟环境。

## 安装

Neovim 插件管理器配置：

```lua
{
  "nirvana6/python-runtime-doc",
  ft = "python",
  opts = {},
  config = function(_, opts)
    require("python_runtime_doc").setup(opts)
  end,
}
```

默认快捷键为 `<leader>K`。如果 Neovim 使用默认 `mapleader`（空格），按 `Space` 后再按 `K`。直接按 `K` 不会被本插件覆盖。

也可以手动加载：

```lua
require("python_runtime_doc").setup()
```

## 配置

```lua
{
  "nirvana6/python-runtime-doc",
  ft = "python",
  opts = {
    keymap = "<leader>K",
    filetypes = { "python" },
    python = nil, -- nil：自动发现；也可填写解释器的绝对路径
    max_width = 100,
    max_height = 24,
    border = "rounded",
    focus = false,
  },
  config = function(_, opts)
    require("python_runtime_doc").setup(opts)
  end,
}
```

解释器选择顺序：

1. `python` 配置项（非空时强制使用）。
2. 从当前文件目录向上最多查找 10 层的 `.venv/bin/python`。
3. `$VIRTUAL_ENV/bin/python`。
4. `PATH` 中的 `python3`。

例如，可显式指定解释器：

```lua
opts = {
  python = "/home/me/project/.venv/bin/python",
}
```

## 使用

### 快捷键查询

在 Python 文件中，将光标放到点号表达式上，按 `<leader>K`：

```python
mujoco.mj_step(model, data)
```

浮窗会显示运行时对象的类型、签名和文档字符串（若库提供）。

### 命令查询

光标放在目标代码上执行：

```vim
:PythonRuntimeDoc
```

也可以直接提供对象的完整限定名：

```vim
:PythonRuntimeDoc mujoco.mj_step
:PythonRuntimeDoc mujoco.MjModel.from_xml_path
```

关闭浮窗使用 Neovim 的常规窗口关闭操作。

## 工作方式与边界

插件通过辅助脚本解析当前 buffer 对应的 Python 源码 AST，使用当前位置的作用域查找简单赋值与类型注解，再在项目解释器中导入目标类型并读取其方法文档。它不会执行当前 Python 文件。当 `__doc__` 为空时，会尝试显示 `inspect.signature()` 的结果。

- 这是运行时文档查询，不是 LSP，不会增加自动补全、类型检查或跳转能力。
- 目标包必须安装在被选中的解释器环境中。
- 第三方库的导入可能执行其初始化代码；查询时应使用可信的 Python 环境和依赖。
- 文档质量取决于库本身；没有 docstring 或签名的对象无法由插件补造说明。
- 裸名称解析是启发式的：它按当前文件的 import 和简单赋值尝试解析；对象方法只支持能够从字面量赋值或类型注解确定接收对象类型的常见写法，暂不分析任意表达式和控制流。点号表达式仍更可靠。
- 对象导入或库初始化失败时，错误会显示在浮窗中。

## 许可证

MIT，详见 [LICENSE](LICENSE)。
