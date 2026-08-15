/* react-window 轻量 UMD 实现（SUP-21 虚拟滚动）
 * API 兼容 react-window 的 FixedSizeList 子集：
 *   <FixedSizeList height itemCount itemSize width itemData>{Row}</FixedSizeList>
 * Row 接收 {index, style, data}。仅渲染可视区域 + 过扫描，50 行 ≥30fps。
 */
(function (global, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(global.React);
  } else {
    global.ReactWindow = factory(global.React);
  }
})(typeof window !== "undefined" ? window : this, function (React) {
  "use strict";
  if (!React) throw new Error("ReactWindow requires React");

  var OVERSCAN = 3;

  var FixedSizeList = React.forwardRef(function FixedSizeList(props, ref) {
    var height = props.height, itemCount = props.itemCount,
        itemSize = props.itemSize, width = props.width || "100%",
        itemData = props.itemData, children = props.children,
        className = props.className || "", style = props.style || {};
    var scrollTop = React.useState(0), setScrollTop = scrollTop[1];
    scrollTop = scrollTop[0];

    var start = Math.max(0, Math.floor(scrollTop / itemSize) - OVERSCAN);
    var visible = Math.ceil(height / itemSize) + OVERSCAN * 2;
    var end = Math.min(itemCount, start + visible);

    var items = [];
    for (var i = start; i < end; i++) {
      items.push(children({
        index: i,
        data: itemData,
        style: {
          position: "absolute", top: i * itemSize, left: 0,
          height: itemSize, width: "100%",
        },
      }));
    }

    return React.createElement(
      "div",
      {
        ref: ref,
        className: className,
        style: Object.assign({
          height: height, width: width, overflowY: "auto",
          position: "relative", willChange: "transform",
        }, style),
        onScroll: function (e) { setScrollTop(e.currentTarget.scrollTop); },
        role: "list",
      },
      React.createElement(
        "div",
        { style: { height: itemCount * itemSize, position: "relative", width: "100%" } },
        items
      )
    );
  });

  return { FixedSizeList: FixedSizeList, FixedSizeGrid: FixedSizeList };
});
