# python-runtime-doc

Neovim 中的 Python 运行时文档查询插件。把光标放在函数或对象上，调用项目 Python 解释器读取它的运行时文档，并在浮窗中查看签名与用途。

它特别适用于没有类型存根（`.pyi` / `py.typed`）的编译型 Python 扩展：静态语言服务器可能只能显示 `Unknown`，但扩展对象的运行时 `__doc__` 仍可能包含签名和说明。

## 功能

- 在 Neovim 浮窗中显示 Python 对象的运行时文档。
- 优先沿当前文件路径向上查找 `.venv/bin/python`，其次使用 `$VIRTUAL_ENV/bin/python`，最后使用 `python3`。
- 支持光标下的点号表达式：既包括 `mujoco.mj_step`，也包括 `history_positions.append` 这样的对象方法。
- 对象方法先按当前文件语法树中查询点之前的名称绑定形式推断接收对象类型，再查询该类型的方法文档：普通/注解/增强赋值、解包、海象（`:=`）、`for`/`with`/`except`/`match` 目标等；例如 `history_positions = []` 推断为 `list`，`with mujoco.viewer.launch_passive(model, data) as viewer` 推断为 `__enter__` 的返回类型。
- 裸名称优先按当前文件中对该名字已做的绑定解析，其次才是 `import` 与内置名称；文件绑定了该名字但类型不可证时直接拒绝，不回落到同名内置对象的文档，保证裸名称与 `名称.属性` 两条查询路径答案一致。
- 类型无法可靠推断时显示原因，不执行当前 Python 文件，也不猜测对象类型。
- 可配置快捷键、Python 解释器路径、浮窗尺寸和文件类型。
- 不依赖 AstroNvim；可用于普通 Neovim 配置。

## 环境要求

- Neovim 0.10 或更新版本（使用 `vim.system`）。
- Python 3.10 或更新版本。
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

在 Python 文件中按一次 `<leader>K`，浮窗会先显示本地运行时文档和签名，底部附带 Google 搜索链接；鼠标左键单击该链接即可打开网页。搜索词由包名和规范 API 名称构成，不包含源码中的局部变量名。若 Neovim 使用默认 `mapleader`（空格），按 `Space` 后按 `K`；普通模式裸 `K` 仍由原有 LSP hover 处理，不会被本插件覆盖。

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

在 Python 文件中，将光标放到 API 表达式上，按 `<leader>K` 查看当前环境可提供的类型、签名和 docstring；若说明不足，鼠标左键单击浮窗底部的 Google 搜索链接打开搜索结果：

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

插件通过辅助脚本把当前 buffer 对应的 Python 源码解析为 AST，按源码顺序对查询行之前的名称绑定做流敏感求值，再从项目解释器中导入目标类型并读取其方法文档。推断只依赖可信的静态线索：赋值语句（普通、注解、增强）、序列解包（定长逐位置、`*starred` → `list`）、海象（`:=`，仅记录当场求值的：`lambda` 或生成器体里的海象要等到调用/迭代时才执行，def 求值点不绑定）、`for`/`async for` 目标（可迭代对象元素类型可证时；循环体内直接取元素类型，循环之后按"零次迭代（体未跑、直接进 `else`）""体正常执行完（再进 `else`）""`break` 退出（跳过 `else`，原样带出 break 点的绑定与先验重绑定）"三条路径合流，全部路径类型一致才给出，否则报告无法推断；`else` 里的 `break` 同样按循环出口处理，`while` 的 `break` 由本层消化、不传播给外层循环）、`with`/`async with` 目标（`__enter__`/`__aenter__` 的返回类型，进入块即绑定，块体中途抛出也不解除它，块后仍保持；返回类型不可证时按新绑定拒绝，绝不回落到块前的旧类型）。块后一行不能证明块体执行完毕：`__exit__`/`__aexit__` 为自定义、未知或 C 级方法时可能返回真值吞掉异常（如 `contextlib.suppress`，也包括 `open()` 这类无法证明返回值的），此时块后状态按"体内首个可能抛出语句之前的安全前缀"与"完整执行"两条路径合流，风险点之后的任何重绑定都不保证存在、按未知报告；仅当能证明 `__exit__` 恒返回假值（注解 `-> None`，或字节码中所有 `return` 常量均为假值）时才按单一"块体跑完"路径处理。体内不可能抛出的语句（字面量赋值、`pass` 等）视为必然执行，其绑定在块后保留；块体内其余的分支式重绑定按合流规则处理，类型不唯一即拒绝）、`except`/`except*` 目标（仅 handler 内；`except*` 绑定为 `ExceptionGroup` 粒度）、`match` 捕获（裸捕获取 subject 类型，`*rest` → `list`，`**rest` → `dict`，不做值模式收窄；多 case 从语句前环境各自出发后合流，书写顺序不决定结果）、参数注解与模块级名称。仅有注解、没有赋值的声明（如 `count: int`）提供的是**声明类型**线索：可用于解析该名字的成员查询，但 PEP 526 下这样的名字在运行时未必已绑定，插件不声称存在该类型的活对象。

它不会执行当前 Python 文件：被查文件只被解析、从不运行；字符串（前向引用）注解一律不求值；`self`/`cls` 因无法在不执行被查文件的前提下导入其所属类而直接拒绝。类型不可证明时显示原因并留空，绝不猜测。

- 这是运行时文档查询，不是 LSP，不会增加自动补全、类型检查或跳转能力。
- 目标包必须安装在被选中的解释器环境中。
- 第三方库的导入可能执行其初始化代码；查询时应使用可信的 Python 环境和依赖。
- 文档质量取决于库本身；没有 docstring 或签名的对象无法由插件补造说明。
- 控制流按路径合流处理，结果类型无法唯一确定时一律报告"无法推断"，不挑一条路径猜类型：`if`/`while` 两分支类型不同或仅单侧绑定；循环结束后的 `for` 目标与只在循环体内出现的绑定（按"零次迭代""体跑完进 `else`""`break` 跳过 `else`"三条路径合流，`break` 路径取 break 点的绑定而不是 `else` 里的赋值；仅当全部路径类型一致，如 `x = 1` 后接 `for x in [2]` 仍给 `int`）；`try` 的 body/`else`/各 handler 体属于互斥路径，只在其中一条上出现的绑定在语句之后拒绝；`match` 跨 case 异型或仅个别 case 绑定的名字（不会取书写顺序上最后一个 case 的类型）；`with` 目标在 `__enter__` 返回类型不可证时的块后查询（不回落到块前的旧类型）；`__exit__` 不可证不吞的 `with` 块中，体内首个可能抛出语句之后的绑定（含体内对已有名字的重绑定，既不信体内新类型也不信块前旧类型，仅合流可证的先验类型）；`lambda`/生成器体内的海象在对象被调用/迭代之前；`del` 之后的名字（旧绑定失效）；handler 结束后的异常变量（CPython 出块即删除）；推导式目标在外层作用域的查询；未注解调用的返回值；无注解参数（默认值不提供类型）；星号导入的名字；不可解析的字符串注解；由 `def`/`class` 语句绑定的局部名称（不在此推断范围内）。
- 裸名称查询与 `名称.属性` 共用同一套绑定证据：当前文件绑定了该名字时，即使类型不可证也拒绝，不会退回去给同名内置对象或导入项的文档。
- 语法覆盖受解析器版本限制：`match` 捕获需 Python 3.10+，`except*` 需 3.11+；在更早的解释器上这些写法本身就无法解析，查询会按解析失败报告。
- 裸名称与对象方法的推断是局部、可证式的，不做跨函数的到达性分析，也不声称能完整静态推断任意 Python 动态语义（如 `getattr`、`exec`、猴子补丁）。异常侧同样只做保守判定，不做全面的异常可达性分析：除字面量赋值、`pass` 这类不可能抛出的语句外，一律按潜在抛点处理；除能证明恒返回假值的 `__exit__` 外，一律按可能吞异常处理——因此只把安全前缀描述为必然执行，识别范围远小于 Python 的完整异常语义（`return`/`raise` 亦未建模为终止语句）。点号表达式配合明确的赋值/注解仍是最可靠的用法。
- 对象导入或库初始化失败时，错误会显示在浮窗中。

## 许可证

MIT，详见 [LICENSE](LICENSE)。
