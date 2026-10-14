# 点位连接显示重叠与方向箭头修正

核查：2026-10-13，Asia/Singapore。用户要求直接核对当前8793页面。已读取实际浏览器状态、SVG端点、连接列表与原接口示例；当前内容属于合成接口验证，不是实际相机/人员通行证据。

## 发现与定位

原结果有A—B、B—C、C—B三条可画记录；后两条端点完全反向重合。页面生成3个SVG line，但只有2条独立几何线，因此C—B记录被盖住，用户看不到重复连接。三条记录均UNCERTAIN_CLOCK_ORDER，不能解读为确定A→B→C→B。

独立核对源坐标为A=(0,0,0)、B=(15,0,7)、C=(30,0,14)，三点共线，投影未把不同原始点错误绑定；连线端点与接口一致。问题是绘制重复端点时没有保留图上可辨认的记录含义。原检查只数元素和比端点，漏了视觉重叠。

按diagnosing-bugs流程先对实际DOM建立红检查（3条描线/2条独立线），再将真实例子的B—C/C—B最小化为两条记录；从原逐条绘制逻辑抽出connectionGroups基线，以下命令已实际运行，4项中2项失败：

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
node --test evidence/2026-10-13-point-connection-display-fix/red-baseline/overlap.test.mjs
```

该红基线是封存的查看工具重现资料，预期退出1；[before-tests.log](before-tests.log)、[before-render.json](before-render.json)、[before截图](before.jpg)保留失败证据。

## 改动

仅改tools/point-viewer的trace.mjs、viewer.mjs、index.html、style.css并新增overlap.test.mjs、direction.test.mjs：同相机对和相同完整3D端点共享几何线，完整records数组保留全部原连接。不同相机对或不同3D端点不会因2D重合而合并。图上编号关联记录，例如#2/#3；单独选择#3高亮对应几何线并保留其顺序状态，其他线变淡，支持图上编号轮换和显示全部。

连接记录数与几何线段数分别显示。第一次重叠修正保留了无方向的—/虚线；用户随后明确要求标箭头，当前版本改为实线加实心箭头表示顺序确定、虚线加空心箭头表示from/to记录方向且实际先后待核实。两个方向分别判断确定性，默认B—C两端有空心箭头；选择记录#3时只突出C→B，图上和可访问说明均随选择变化。箭头尖端避开点位圆圈，避免被盖住。没有移动点位、改样例坐标或编造曲线路径。图形保持原坐标投影，原序列、身份/时间合同和API字段不变。

## 验证与交接

```powershell
node --test tools/point-viewer/trace.test.mjs tools/point-viewer/overlap.test.mjs tools/point-viewer/direction.test.mjs
node --check tools/point-viewer/viewer.mjs
```

当前[final-tests.log](final-tests.log)16项通过，含4项重叠回归、4项方向回归和原8项检查，viewer.mjs语法检查通过。浏览器原症状检查重新执行：2条无完全重叠描线、完整保留记录[0,1,2]、图上#1和#2/#3；当前方向见[after-direction-render.json](after-direction-render.json)，默认3个空心箭头、0个实心箭头，选择第3条为反向空心箭头、无正向箭头，实际点选/编号轮换/恢复全部均通过。截图：[当前全部连接](after-direction.jpg)、[当前第3条选中](selected-record-3-direction.jpg)。

历史阶段证据保留：[after-render.json](after-render.json)、[after-tests.log](after-tests.log)、[after.jpg](after.jpg)、[selected-record-3.jpg](selected-record-3.jpg)是用户追加方向要求之前的重叠修正，12项通过、不确定箭头为0。这些文件不代表当前箭头版本；旧失败快照同样不覆盖。

当前8793静态服务已读取新文件，浏览器已刷新。没有访问生产、相机或更改业务数据；C++/Go/OpenAPI/程序及sample.json哈希与原清单一致，本次没有重新跑它们的旧验证。原第32节清单代表修正前快照，当前查看工具SHA见[final-manifest.json](final-manifest.json)。用户可点击编号逐条检查；真实场地正确性仍需实际坐标、独立同人和时间证据。

预防经验：几何数量不能等同连接记录数量，SVG元素数不能代表可辨认的绘图结果；回归要校验全部记录编号覆盖、同端点往返、单次选择、方向不确定语义与截图。connectionGroups作为独立可测试的绘制边界已保留，无临时调试日志；red-baseline仅在明示的调试证据目录。

用户追加询问模型接入后是否能画路线：已解释本地合同可画确认抓拍点位之间的连接，前提为同人确认、可信时间、活动/device绑定及相机坐标；模型本身不能替代这些数据。真实相机→P1→P2→查看器完整链路未验证，本例不证明现场准确性，点位直线不证明实际中间通路，鹰眼单镜连续轨迹仍后置。接手时先补真实输入与独立身份/时间/点位真值，沿用当前签名monitoring入流和查询合同验收。
