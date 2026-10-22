from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = ROOT / "src/components/PluginParamsForm.tsx"
PIPELINE = ROOT / "src/pages/channels/components/PipelineView.tsx"
SHEET = ROOT / "src/components/InterceptorSheet.tsx"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def assert_contains(source: str, needle: str, message: str) -> None:
    if needle not in source:
        raise AssertionError(message)


# 修改原因：插件参数从自由文本升级为 metadata.params_schema 驱动的可视化表单，需要防止后续重构遗漏任一接入点。
# 修改方式：用轻量源码断言检查共享类型、解析序列化函数、五类控件、visible_when 和两个使用位置。
# 目的：在没有前端测试框架的项目里，为本次 UI 结构提供可重复的最低成本回归检查。
form_source = read(FORM)
pipeline_source = read(PIPELINE)
sheet_source = read(SHEET)

assert_contains(form_source, "export interface ParamSchema", "应导出 ParamSchema 类型")
assert_contains(form_source, "parsePluginOptions", "应导出 parsePluginOptions")
assert_contains(form_source, "serializePluginOptions", "应导出 serializePluginOptions")
assert_contains(form_source, "PluginParamsForm", "应导出 PluginParamsForm 组件")
assert_contains(form_source, "visible_when", "应支持 visible_when 条件显示")
assert_contains(form_source, "type=\"number\"", "应渲染 number 输入框")
assert_contains(form_source, "<select", "应渲染 select 控件")
assert_contains(form_source, "Switch.Root", "应渲染 toggle 开关")
assert_contains(form_source, "multiple", "应渲染 multi-select 控件")
assert_contains(form_source, "key=value", "应保留 key=value 模式说明")
assert_contains(form_source, "positional", "应保留 positional 模式说明")

assert_contains(pipeline_source, "PluginParamsForm", "PipelineView 应使用可视化参数表单")
assert_contains(pipeline_source, "metadata?.params_schema", "PipelineView 应读取 metadata.params_schema")
assert_contains(sheet_source, "PluginParamsForm", "InterceptorSheet 应使用可视化参数表单")
assert_contains(sheet_source, "metadata?.params_schema", "InterceptorSheet 应读取 metadata.params_schema")

print("plugin params form source checks passed")
