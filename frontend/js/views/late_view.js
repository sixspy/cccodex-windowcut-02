export const lateViewTemplate = `
<div class="card wide" v-show="tab===2">
  <h2>迟到桶</h2>
  <p class="muted">关窗后到达进桶；补入走甲路径·整笔同事务（删桶行+插正式同窗提交，失败两侧回滚），作废仍为空桩。</p>
  <p>合计 {{lateSummary.count||0}} 笔 / 金额 {{lateSummary.amount||0}}</p>
  <p class="muted">补入口径：{{promoteRegisterText}}（迟到补入册·当天禁止改口，偏册拒写记「补入偏了」）</p>
  <p>
    <label>目标窗
      <select v-model="promoteTarget">
        <option v-for="w in windows" :key="w.id" :value="w.name">{{w.name}} · {{w.status}} · {{w.open_at}}~{{w.close_at}}</option>
      </select>
    </label>
    <span class="muted">目标窗须开启且事件时间在窗内，否则拒写</span>
  </p>
  <div class="err">{{actionErr}}</div>
  <table>
    <thead><tr><th>窗口</th><th>键</th><th>时间</th><th>金额</th><th>原因</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="e in lateItems" :key="'l'+e.id">
        <td>{{e.window_name}}</td>
        <td>{{e.biz_key}}</td>
        <td>{{e.event_ts}}</td>
        <td>{{e.amount}}</td>
        <td>{{e.reason}}</td>
        <td>
          <button class="ghost" @click="promoteLate(e.id)">补入</button>
          <button class="ghost" @click="discardLate(e.id)">作废空桩</button>
        </td>
      </tr>
    </tbody>
  </table>
</div>
`;
