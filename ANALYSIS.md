# LazyConfigValue、default 与 validate 的求值次序及缓存边界

本文追踪一个配置值从 `Config` 合并到 `HasTraits` 实例首次访问的全链路，
固定同一个值在 class default、`@default`、配置增量操作、`@validate` 和
observer 之间的先后顺序，并标明每一级缓存的边界与错误传播路径。
对应实现位于 `traitlets/config/loader.py`（本次修改）与
`traitlets/config/configurable.py`、`traitlets/traitlets.py`（未修改，
仅作为链路上下文描述）。定向回归测试在
`tests/config/test_lazy_eval_order.py`。

## 入口与状态

配置数据在到达实例之前经历三种载体：

1. **`Config` / section**：`cfg.Base.xs` 中 `Base` 是 section（`Config`
   子字典），`xs` 下的增量调用（`append/extend/prepend/insert/update/add`）
   不立即求值，而是累积进一个 **`LazyConfigValue`**（`loader.py`）。
   它内部的状态是五组操作记录：`_extend`、`_prepend`、`_inserts`、
   `_update`（dict 或 set），以及仅供 repr 调试的 `_value`。
2. **合并**：`Config.merge(other)` 把 `other` 的 section 递归并入自身。
   两边同一 key 都是 `LazyConfigValue` 时，调用
   `deepcopy(v).merge_into(self[k])`：`other`（高优先级）的拷贝吸收
   `self[k]`（低优先级）的操作，吸收顺序为 extend/insert 早者在前、
   prepend 晚者在前、update 晚者覆盖同名字段。**两个源 Config 的惰性
   对象在合并后保持原样、互不别名**——这是本次修复的边界之一
   （`test_multiple_config_merge_does_not_mutate_source`、
   `test_merge_into_leaves_earlier_lazy_intact`）。
   注意 section 与普通值仍遵循既有的 no-copy 语义
   （`test_merge_no_copies` 钉住：`target` 缺少某 section 时直接别名
   `source` 的 section 对象），只有 `LazyConfigValue` 这种"可变累积器"
   被复制。
3. **实例化**：`Configurable.__init__` 在 `hold_trait_notifications()`
   里调用 `_load_config`：先 `_find_my_config` 按 `section_names()`
   （`reversed(mro)`，即 父类 section 先、子类 section 后）合并出本实例
   的配置——**子类 section 覆盖父类 section，同名惰性操作跨 section
   复合**（`test_class_hierarchy_config_sections_compound`）。

## 求值次序（config 加载路径）

对每一个 `config=True` 且出现在配置里的 trait，顺序为：

1. `initial = getattr(self, name)` —— 触发 **default 求值**：
   静态 `default_value`（class default）或 `@default` 动态默认值，
   经类型校验（此时 `_cross_validation_lock=True`，**不跑 `@validate`**），
   结果写入实例缓存 `_trait_values[name]`，并直接发出
   `type="default"` 通知（不经过 hold 压缩；`@observe` 默认只监听
   `type="change"`，故一般不可见）。
2. `config_value.get_value(initial)` —— `LazyConfigValue` 以该默认值
   为基**现场具体化**：深拷贝 `initial`，依次应用 inserts → prepend →
   extend（list）或 update（dict/set）。**结果不跨调用缓存**；`_value`
   只记录最近一次具体化供 repr 诊断，永不回供。
3. `setattr(self, name, deepcopy(config_value))` —— 深拷贝保证实例间
   不共享容器；`TraitType.set` 跑类型校验（hold 期间不跑 `@validate`），
   写入 `_trait_values`，change 通知被 hold 压缩（同名多次 change 合并，
   保留首个 old、末个 new）。
4. hold 退出时：对每个变更过的 trait 取**最终值**跑一次
   `_cross_validate`（即 `@validate`），随后 `set_trait` 并补发压缩后的
   change 通知，observer 此刻触发。

实测事件序列（`test_config_load_callback_order`）：

    default:xs  ->  validate:xs:[1, 2]  ->  observe:xs:change:[1]->[1, 2]

即：**class default / `@default`（合并基） → 配置增量具体化 →
`@validate`（仅一次，作用于合并后最终值） → observer**。

## 三条入口路径对比

| 路径 | `@default` | 配置增量 | `@validate` | observer |
|---|---|---|---|---|
| 类层级继承（config 加载） | 先跑，提供合并基 | 父/子 section 复合后应用 | hold 退出时对最终值跑一次 | 最后触发，old=默认值 |
| 多个 Config 叠加 | 同上 | 按 merge 顺序复合，后并者高优先 | 同上 | 同上 |
| 首次读（无配置） | 跑，结果缓存于 `_trait_values` | 无 | **不跑**（cross-validation 锁） | 仅 `type="default"` 事件 |
| 显式赋值 | 不跑（除非此前读过） | 无 | 赋值时同步跑一次 | 校验后触发 |

首次读与显式赋值的两个可观测差异（均有测试钉住）：

- 首次读的默认值**不经过 `@validate`**，且只求值一次后缓存
  （`test_first_read_callback_order`）。
- 容器 trait 的 default 是动态拷贝（`default_value` 保持 `Undefined`），
  因此未先读就显式赋值时，change 事件的 `old` 是 `Undefined`；
  先读后赋值则 `old` 为已物化的默认值
  （`test_explicit_assignment_callback_order`）。

## 缓存边界

- **`_trait_values`（每实例）**：default 与赋值结果的唯一定点缓存；
  实例之间天然隔离。
- **`LazyConfigValue._value`（每惰性对象）**：仅 repr/诊断用。修复前它
  被 `get_value` 当作跨调用缓存回供，是第一个实例的合并基污染后续所有
  实例的根因；修复后每次调用都从传入的 `initial` 重新具体化
  （`test_get_value_reifies_fresh_container_per_call`）。
- **`Configurable._load_config` 的 `deepcopy`**：实例值的最后一道隔离，
  保证两个实例不共享同一个容器对象
  （`test_lazy_value_not_shared_between_instances`）。
- **`Config.merge`**：section/普通值 no-copy（既有设计），
  `LazyConfigValue` 深拷贝后合并（新边界）。

## 错误传播

- `@validate` 在 config 加载的 hold 退出阶段抛 `TraitError` 时，
  `hold_trait_notifications` 回滚已暂存的变更（能恢复旧值的恢复旧值，
  无旧值的从 `_trait_values` 移除），异常原样抛出并携带 trait 名与
  实例上下文（`test_validate_error_during_config_load_rolls_back`）。
- 类型校验失败（如把不可转换的值赋给 `List`）在 `setattr` 同步抛出
  `TraitError`，信息包含 trait 名、所属类与期望类型。
- `TraitType.get` 对"默认值未正确设置"的意外异常统一包装为
  `TraitError("Unexpected error in TraitType: ...")` 并链式保留原异常。

## 最危险反例（已转为回归测试）

一个 `Config`（`cfg.Base.xs.append(2)`）同时作用于 `Base`（默认 `[1]`）
和重写了 `@default` 返回 `[10]` 的 `Child(Base)`。修复前，
`LazyConfigValue.get_value` 的缓存把**第一个**实例化的类的合并基烤进
缓存：若 `Base` 先实例化，`Child` 也得到 `[1, 2]` 而非 `[10, 2]`——
配置合并、descriptor 求值与回调顺序三者在此交叉放大。修复后两个类各自
用自己的默认值做合并基，与实例化顺序无关。
回归用例：
`tests/config/test_lazy_eval_order.py::test_lazy_value_respects_subclass_default`
（dict 对照：
`test_lazy_dict_update_respects_subclass_default`）。
