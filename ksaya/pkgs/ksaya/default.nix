# KSAYA 稳定核心 —— 作为 Nix 定制系统中的一个正规包（derivation）
#
# 设计目标：不再用 environment.etc 逐个下发散文件，而是把 KSAYA 整体构建成一个
# 不可变、可复现、可审计的 store 路径 /nix/store/...-ksaya-<version>，由模块引用。
#
# 关键约束（踩坑记录）：
#   1. /nix/store 全树只读 —— 脚本里写 state/日志必须外置。
#      所以 ksayactl.py/regression.sh 支持 KSAYA_CONFIG/KSAYA_STATE_DIR/KSAYA_LOG 环境变量，
#      由 ksaya.nix 指向 /etc/ksaya、/var/lib/ksaya（见 modules/ksaya.nix）。
#   2. ksayactl.py 用 importlib 按 __file__ 同目录加载 ksaya-bandwidth.py（文件名带连字符
#      不能 import），所以布局必须保持「所有 .py 平铺在同一目录」。
#   3. Python 只用标准库 + PyYAML；不要引入额外依赖，保持可离线复现。
#   4. 用 makeWrapper 把 python3 + pyyaml 固化进 wrapper，运行时不依赖宿主的 python3
#      （实测坑：node1 的 python3 不在 PATH 里，之前要写死 /nix/store/.../bin/python3）。

{ lib
, stdenv
, python3
, bash
, coreutils
, makeWrapper
, version ? "1.0"
}:

# Python 运行时只含「标准库 + PyYAML」。必须用 let 绑定而不是写成 derivation 的
# 兄弟属性 —— installPhase 的字符串插值只能看到词法作用域里的变量，
# 看不到同一属性集里的其他属性（那个集不是 rec），写成兄弟属性会直接求值报
# undefined variable 'pythonEnv'。
let
  pythonEnv = python3.withPackages (ps: [ ps.pyyaml ]);

  # 运行时默认路径。
  #
  # 为什么必须给 wrapper 默认值，而不是只靠 modules/ksaya.nix 的
  # environment.sessionVariables：
  #   sessionVariables 只会写进 /etc/profile，对非登录 shell（bash -c、systemd、
  #   cron）完全无效。那种场景下环境变量为空，ksayactl 会退回「按 __file__ 找
  #   同目录的 state/」——而那是 /nix/store 里的只读路径，写入必然失败。
  #   所以把正确默认值钉进 wrapper 环境，才是唯一可靠的兜底。
  #
  # ⚠️ 这两个值必须与 modules/ksaya.nix 的 stateDir / configFile 默认值一致。
  defaultConfig = "/etc/ksaya/ksaya.config.yaml";
  defaultStateDir = "/var/lib/ksaya";
in
stdenv.mkDerivation {
  pname = "ksaya";
  inherit version;

  # 源直接取自仓库内 ksaya-stable/（单一真值源），不做网络拉取，保证离线可构建。
  #
  # ⚠️ 必须过滤：运行回归会在 ksaya-stable/ 里生成 regression*.log、__pycache__、state/、
  # bench-*.json（基准运行时产物）；若不过滤，这些运行时产物会进 store，导致每次
  # 构建哈希都变、彻底破坏可复现性。
  src = lib.cleanSourceWith {
    src = ../../ksaya-stable;
    filter = path: type:
      let base = baseNameOf path; in
      !(type == "directory" && (base == "__pycache__" || base == "state"))
      && !(lib.hasSuffix ".pyc" base)
      && !(lib.hasSuffix ".log" base)
      # 只排除基准的 .json 运行时产物（不进 store）。⚠️ 不能用 hasPrefix "bench-"：
      # 那样会把 bench-profiles.py / bench-stats.py / bench-sensitivity.py 三个
      # 源码也一起排除，installPhase 的 `install "$src/bench-profiles.py"` 就会
      # 「文件不存在」而失败（云端 nix build 真抓出过这个错）。
      && !(lib.hasPrefix "bench-" base && lib.hasSuffix ".json" base)
      && !(base == ".DS_Store");
  };

  # 集群 workload 清单单独作为一个源（它们在 ksaya-demo/，与核心解耦）
  demoSrc = lib.cleanSourceWith {
    src = ../../ksaya-demo;
    filter = path: type:
      let base = baseNameOf path; in
      !(base == ".DS_Store");
  };

  nativeBuildInputs = [ makeWrapper ];

  # 不需要编译步骤：KSAYA 是纯 Python + Shell 的声明式控制器。
  dontBuild = true;
  dontConfigure = true;

  # 关闭 fixup 阶段里会破坏脚本的东西，但保留 shebang patch。
  dontStrip = true;


  installPhase = ''
    runHook preInstall

    out_py=$out/lib/ksaya
    out_bin=$out/bin
    mkdir -p "$out_py" "$out_bin" "$out/share/ksaya"

    # --- 核心 Python（必须平铺：ksayactl 用 __file__ 同目录加载 ksaya-bandwidth.py）---
    for f in ksayacore.py ksayactl.py ksaya-bandwidth.py ksaya-capacity.py capability.py \
             ksaya-profiles.py bench-profiles.py bench-stats.py bench-sensitivity.py \
             bench-router-throughput.py bench-triple-gap.py; do
      install -m 0644 "$src/$f" "$out_py/$f"
    done

    # --- 单一真值源与数据文件（只读副本，运行时以 /etc/ksaya 的为准）---
    #
    # ⚠️ 安全边界：/nix/store 是全体用户可读的，任何进 store 的东西都等同于公开。
    #   ksaya.config.yaml / quota-capacity.json / ksaya-accounts.json 目前都**不含凭证**
    #   （access_key/secret_key 语义已随通用云兼容层一起删除），只含拓扑与配额，
    #   所以进 store 无碍。
    #   🚫 铁律：将来若需要引入任何密钥，绝不能塞进这三个文件 —— 必须改走
    #   systemd EnvironmentFile + agenix/sops 注入到 /etc 或 /run，永不进 store。
    for f in ksaya.config.yaml quota-capacity.json ksaya-accounts.json; do
      install -m 0444 "$src/$f" "$out_py/$f"
    done

    # --- 测试与工具脚本 ---
    install -m 0644 "$src/regression.sh" "$out_py/regression.sh"
    install -m 0755 "$src/enroll.sh" "$out_bin/ksaya-enroll"
    install -m 0755 "$src/ksaya-report.sh" "$out_bin/ksaya-report"

    # --- MC 相关（验收夹具；不作为核心依赖）---
    # 注意：不能写 `[ -f x ] && install ...`。stdenv 的 set -e 会让「文件不存在时
    # 整个 && 表达式返回 1」直接中断构建，而这里恰恰允许可选文件缺失 —— 必须用显式 if。
    mkdir -p "$out_py/mc"
    for f in mc/mc_playtest.py mc/packets-777.json mc/server.properties \
             mc/build-mc-image.sh mc/build-eagler-image.sh; do
      if [ -f "$src/$f" ]; then
        install -m 0644 "$src/$f" "$out_py/$f"
      fi
    done
    mkdir -p "$out_py/tests"
    if [ -f "$src/tests/test_ksayacore.py" ]; then
      install -m 0644 "$src/tests/test_ksayacore.py" "$out_py/tests/test_ksayacore.py"
    fi

    # --- 集群 workload 清单（从 ksaya-demo/ 收进同一个包，避免散装分发）---
    mkdir -p "$out/share/ksaya/manifests"
    for f in "$demoSrc"/*.yaml "$demoSrc"/*.sh; do
      [ -f "$f" ] || continue
      base=$(basename "$f")
      case "$base" in
        *.yaml) install -m 0444 "$f" "$out/share/ksaya/manifests/$base" ;;
        *.sh)   install -m 0555 "$f" "$out/share/ksaya/manifests/$base" ;;
      esac
    done

    # --- 可执行入口：把 python3 + PyYAML 固化进 wrapper ---
    # 运行时不依赖宿主 PATH 里的 python3（Node1 实测没有）。
    # --set-default：环境变量优先（systemd unit / 用户 export 仍可覆盖），
    # 但没设时用包内的正确默认值，绝不落回只读的 store 目录。
    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksayactl" \
      --add-flags "$out_py/ksayactl.py" \
      --set KSAYA_VERSION "${version}" \
      --set-default KSAYA_CONFIG "${defaultConfig}" \
      --set-default KSAYA_STATE_DIR "${defaultStateDir}" \
      --set-default KSAYA_PACKAGE_DIR "$out_py" \
      --prefix PATH : "${lib.makeBinPath [ bash coreutils ]}"

    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksaya-capacity" \
      --add-flags "$out_py/ksaya-capacity.py"

    # 原始入口（调试/排障用）：从包内副本直接跑 core 的自测，不经过 ksayactl。
    # 这就是「KSAYA 真的装在系统里」的自证手段。
    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksaya-core-selftest" \
      --add-flags "$out_py/ksayacore.py" \
      --set-default KSAYA_CONFIG "${defaultConfig}" \
      --set-default KSAYA_STATE_DIR "${defaultStateDir}" \
      --set-default KSAYA_PACKAGE_DIR "$out_py"

    # 三版本基准入口：同一 workload 跑 baseline/xnu-qos/oh-distributed
    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksaya-benchmark" \
      --add-flags "$out_py/bench-profiles.py" \
      --set-default KSAYA_CONFIG "${defaultConfig}" \
      --set-default KSAYA_STATE_DIR "${defaultStateDir}" \
      --prefix PATH : "${lib.makeBinPath [ bash coreutils ]}"

    # 统计检验入口：对 bench-*.json 做配对显著性检验
    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksaya-bench-stats" \
      --add-flags "$out_py/bench-stats.py" \
      --prefix PATH : "${lib.makeBinPath [ bash coreutils ]}"

    # 调度器 wall-clock 吞吐入口：测「调度算法本身」的真实 CPU 开销
    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksaya-router-throughput" \
      --add-flags "$out_py/bench-router-throughput.py" \
      --prefix PATH : "${lib.makeBinPath [ bash coreutils ]}"

    # 三重差距基准入口：性能/带宽/地域极大差异下的调度对照
    makeWrapper "${pythonEnv}/bin/python3" \
      "$out_bin/ksaya-triple-gap" \
      --add-flags "$out_py/bench-triple-gap.py" \
      --prefix PATH : "${lib.makeBinPath [ bash coreutils ]}"


    # regression.sh 走 bash；它内部会调 python3，必须让 PATH 里有带 pyyaml 的那个。
    # 回归会在 state 目录里写日志，所以同样要兜住 KSAYA_STATE_DIR / KSAYA_LOG。
    makeWrapper "${bash}/bin/bash" \
      "$out_bin/ksaya-regression" \
      --add-flags "$out_py/regression.sh" \
      --set-default KSAYA_CONFIG "${defaultConfig}" \
      --set-default KSAYA_STATE_DIR "${defaultStateDir}" \
      --set-default KSAYA_LOG "${defaultStateDir}/regression.log" \
      --prefix PATH : "${lib.makeBinPath [ bash coreutils ]}" \
      --prefix PATH : "${pythonEnv}/bin"

    # =====================================================================
    # 安装自检：构建期就把「包自洽」验证掉，而不是等部署到节点才发现。
    # 这是 derivation 的关键好处 —— 坏包构建不出来，不会污染节点。
    # （不是 checkPhase：dontBuild=true 时 phases 已经跑过去了，放 installPhase 尾最稳。）
    # =====================================================================
    echo "=== KSAYA install self-check ==="
    # ⚠️ 构建期还没有 /etc/ksaya，必须显式指向包内副本，否则 validate 会因
    # 「配置文件不存在」而失败（这正是 wrapper 默认值的盲区，不能靠默认值）。
    # 状态目录同样指到临时目录：sandbox 里写不了 /var/lib。
    chk_cfg="$out_py/ksaya.config.yaml"
    chk_state="$(mktemp -d)"
    KSAYA_CONFIG="$chk_cfg" KSAYA_STATE_DIR="$chk_state" \
      "$out_bin/ksayactl" --version
    KSAYA_CONFIG="$chk_cfg" KSAYA_STATE_DIR="$chk_state" \
      "$out_bin/ksayactl" validate >/dev/null && echo "self-check: validate OK"
    # 必须能 import 同目录的 ksaya-bandwidth.py（文件名带连字符，走 importlib）
    # ⚠️ 实测坑：spec_from_file_location 之后必须把模块塞进 sys.modules 再
    # exec_module，否则 @dataclass 装饰器会在 _is_type 里做
    # sys.modules[cls.__module__] 反向查找，拿到 None 直接
    # AttributeError: 'NoneType' object has no attribute '__dict__'。
    # 这是 dataclasses 的已知行为，不是模块本身有问题。
    KSAYA_PACKAGE_DIR="$out_py" "${pythonEnv}/bin/python3" - <<'PYCHK'
import importlib.util, os, sys
d = os.environ["KSAYA_PACKAGE_DIR"]
for mod, fn in [("ksayabw", "ksaya-bandwidth.py"), ("ksayacap", "ksaya-capacity.py"), ("capability", "capability.py")]:
    p = os.path.join(d, fn)
    if not os.path.exists(p):
        print(f"self-check: MISSING {fn}"); sys.exit(1)
    spec = importlib.util.spec_from_file_location(mod, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m          # ← 关键：dataclass 需要能反查到本模块
    spec.loader.exec_module(m)
print("self-check: modules importable OK")
PYCHK
    # 确认核心单测随包一起带上了（回归入口要用）
    test -f "$out_py/tests/test_ksayacore.py"
    echo "=== KSAYA install self-check done ==="

    runHook postInstall
  '';

  # 让 `nix run`/模块里的 ${ksaya}/bin/ksayactl 可直接用
  meta = with lib; {
    description = "KSAYA 集群基石系统：把多节点虚拟成一台大电脑，智能拆分任务并路由到各节点";
    homepage = "https://example.invalid/ksaya";
    license = licenses.mit;
    mainProgram = "ksayactl";
    platforms = platforms.linux;
  };
}