# ANALYSIS — LazyConfigValue、default 与 validate 的求值次序及缓存边界

本文追踪 traitlets 中一个配置值从 `Config` 合并到 `HasTraits` 实例首次访问的全链路，
固定 class default、`@default`、配置增量操作（`LazyConfigValue`）、`@validate` 与
observer 之间的先后顺序，并说明每一步的状态、缓存位置与错误传播方式。

## 1. 入口（entry points）

同一个“配置值 → 实例 trait”的链路有四个入口，全部汇聚到
`Configurable._load_config`（`traitlets/config/configurable.py`）：

| 入口 | 触发 |
| --- | --- |
| `Configurable.__init__(config=c)` | `self.config = config` 触发 `_config_changed` → `_load_config` |
| `Configurable.__init__()`（无 config） | `self._load_config(self.config)`（来自 `_config_default` 的空 Config） |
| `obj.update_config(c)` | `self.config = deepcopy(self.config)`（再次触发 `_config_changed`）+ 显式 `_load_config(c)`，随后 `self.config.merge(c)` |
| `obj.config = c2` | `_config_changed` observer → `_load_config` |

`_load_config` 之前还有两段纯 Config 侧的链路：

1. **增量记录**：配置文件里 `c.C.items.append(1)` 访问小写键时，
   `Config.__getattr__` 创建 `LazyConfigValue`（`traitlets/config/loader.py`），
   只记录操作（`_extend` / `_prepend` / `_inserts` / `_update`），不计算容器。
2. **Config 叠加**：`Config.merge(other)` 对惰性值调用
   `v.merge_into(self[k])`；`Configurable._find_my_config` 还会按 MRO
   （基类 section 在前，子类 section 优先）把多个 section 合并成 `my_config`。

## 2. 全链路求值次序

### 2.1 首次读（无配置或读取未设置的 trait）

`TraitType.get`（`traitlets/traitlets.py`）：

1. `obj.trait_defaults(name)`：class default（`List([0])` 的 `default_value`）
   或 `@default` 生成器；容器 trait 的 default 经 `Instance.make_dynamic_default`
   逐实例复制（`list([0])`），所以每个实例的初始容器对象不同；
2. `self._validate(obj, default)`：此时 `get` 持有 `_cross_validation_lock`，
   因此只跑 trait 自身的类型校验（`List.validate`），**不跑 `@validate`**；
3. 结果写入实例缓存 `obj._trait_values[name]` —— 这是实例级缓存边界，
   之后再次读取不再触发任何回调；
4. `_notify_observers(type="default")`：**同步**直发，不经过
   `hold_trait_notifications` 的挂起队列（它替换的是 `notify_change`，
   而 `get` 直接调 `_notify_observers`）。

实测事件序列：`default → observe:default`（无 `validate`）。

### 2.2 配置加载（`_load_config`，整体在 `hold_trait_notifications` 内）

对每个 `config=True` 的惰性值：

1. `initial = getattr(self, name)` —— 触发 2.1 的首次读（default 求值并缓存）；
2. `_reify_lazy_config_value(name, lazy, initial)`：
   - 若该实例已应用过这批增量（按 `_lineage` 判断），直接返回当前值，不重复应用；
   - 否则 `lazy.get_value(initial)`：`deepcopy(initial)` 后按序应用
     inserts → prepend → extend（list）或 update（dict/set），
     **纯函数，不在共享惰性对象上留任何状态**；
3. `setattr(self, name, deepcopy(value))` → `TraitType.set` → `_validate`
   （锁仍持有，只做类型校验）→ 写入 `_trait_values` → 变更通知进入挂起队列；
4. `hold_trait_notifications` 退出时：对每个缓存的名字跑 `_cross_validate`
   —— **`@validate` 在此刻、且仅在此时，作用于“default + 增量”的最终值，
   恰好一次**；随后重放挂起的通知（先 `default` 型，后 `change` 型，
   `change.old = 默认值`，`change.new = 配置合并值`）。

实测事件序列：`default → observe:default → validate([0,1]) → observe:change([0,1])`。

### 2.3 显式赋值

`obj.foo = [5]` → `TraitType.set`：类型校验 → `@validate`（锁空闲，立即执行）
→ 写缓存 → 同步触发 `observe:change`。构造器 kwargs 也属于显式赋值：
`HasTraits.__init__` 先设置 kwargs，配置加载后 `Configurable.__init__`
会把同时出现在 kwargs 与 config 中的 trait 用 kwargs 再覆盖一次
（`config_override_names`），所以 `C(config=c, foo=[5])` 恒为 `[5]`。

### 2.4 类层级继承

`_find_my_config` 依 `section_names()`（`reversed(mro)`，基类在前）依次
`merge` 各 section：子类 section 的纯值覆盖基类，子类 section 的惰性增量
追加在基类增量之后。`c.Base.foo.append(1)` + `c.Derived.foo.append(2)`
对 `Derived(config=c)` 得 `[0, 1, 2]`，对 `Base(config=c)` 得 `[0, 1]`
（基类看不到子类 section，且合并不再污染基类 section 的惰性对象）。

## 3. 状态与缓存边界

| 状态 | 位置 | 生命周期 |
| --- | --- | --- |
| 增量操作（`_extend` 等） | `LazyConfigValue`，存在共享 `Config` 里 | 与 Config 相同；纯记录，可安全共享 |
| `_lineage`（frozenset of int） | 每个 `LazyConfigValue`；`merge_into` 取并集，`deepcopy` 保留 | 标识“这批增量”，供实例判重 |
| 已应用集合 `_lazy_config_applied` | **实例** `__dict__`（`{trait名: set(lineage)}`） | 实例级缓存边界；重复加载幂等 |
| 最终值 | 实例 `_trait_values[name]` | 实例级；`get` 的读取缓存 |
| 挂起通知 | `hold_trait_notifications` 的局部 `cache` | 上下文退出时压缩重放 |

修复前（上游 5.16.1 及本快照初始状态）的两处越界：

1. `LazyConfigValue.get_value` 把结果缓存在共享对象的 `self._value` 上：
   第一个实例的 default 会泄漏给所有后续实例（含不同 default 的子类、
   含逐实例动态 default 的同类实例）。
2. `merge_into` 原地修改低优先级操作数并与其共享 list/set/dict：
   `c.merge(c1); c.merge(c2)` 会污染 `c1`/`c2` 中的惰性对象，
   甚至通过 `_find_my_config` 的 section 合并污染**基类 section**
   （`Derived` 的增量泄漏进 `Base` 的实例）。

## 4. 错误传播

- 配置值类型错误：`setattr` 时 `TraitType.validate` 抛 `TraitError`，
  消息含 trait 名与属主类；`hold_trait_notifications` 捕获后回滚已挂起的
  变更再向上传播，实例不留半配置状态。
- `@validate` 在配置加载末尾（hold 退出时）抛错：同样回滚并传播，
  异常消息原样保留（可诊断上下文不被吞掉）。
- 增量类型与 default 容器类型不匹配（如对 dict default 记录 append）：
  `get_value` 忽略不匹配的增量，返回未修改的 default —— 这是文档化的
  边界行为，不抛错也不损坏默认值。
- `LazyConfigValue.insert` 非整数索引：`TypeError("An integer is required")`。

## 5. 输出（可观察结果）

- 实例 trait 值 = `f(实例自己的 default, 累计增量)`，与加载次数无关（幂等）；
- 两个实例/两个类共享一个 `Config` 互不可见对方的 default 与后续修改；
- `Config.merge` 的源对象保持可继续独立使用；
- observer 顺序固定：`observe:default`（同步）→ `validate` → `observe:change`。

## 6. 反证（executable counterexamples）

`tests/config/test_lazy_config_evaluation.py` 中以下用例在修复前的代码上失败
（已用 `git stash` 验证，7 个失败）：

- `test_two_instances_per_instance_dynamic_default` —— 最危险反例，见 CHANGELOG；
- `test_lazy_object_has_no_reified_state_after_load`、`test_merge_into_is_pure_function`、
  `test_merge_does_not_mutate_or_alias_sources`、`test_stacked_configs_prepend_extend_and_update`、
  `test_class_hierarchy_sections_compose_in_mro_order`、
  `test_get_value_type_mismatch_leaves_initial_unchanged`。
