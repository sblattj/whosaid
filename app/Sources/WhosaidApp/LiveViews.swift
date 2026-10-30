import SwiftUI
import WhosaidKit

/// Live now: jibbr's five lanes (Transcript, Topics, Wordgraph, Summaries, Next steps)
/// over the newest `whosaid live` session, or the wordgraph alone, full window.
struct LiveWorkspaceView: View {
  @Environment(AppModel.self) var model

  var body: some View {
    let live = model.live
    VStack(spacing: 0) {
      LiveHeader()
      Divider()
      if let s = live.session {
        if live.fullWordgraph {
          WordgraphLane(insights: s.insights, full: true)
        } else {
          LaneStack(
            transcript: TranscriptLane(entries: s.entries, active: live.active),
            topics: TopicsLane(insights: s.insights),
            wordgraph: WordgraphLane(insights: s.insights, full: false),
            summaries: SummariesLane(insights: s.insights),
            nextSteps: NextStepsLane(insights: s.insights))
        }
      } else {
        EmptyState(symbol: "dot.radiowaves.left.and.right", title: "No live transcript yet",
                   message: "Start `whosaid live` while a call is being recorded (autorecord included). Lines, topics and next steps show up here as the call goes on.",
                   action: nil)
      }
    }
    .onAppear { live.lanesVisible = true }
    .onDisappear { live.lanesVisible = false }
    .task(id: live.stamp) { await live.refreshInsights() }
  }
}

/// The five lanes side by side. Each lane but the wordgraph has a drag handle on its
/// right edge and remembers its width; the wordgraph takes what is left. (HSplitView
/// sized its panes past the window's edge, hiding the sidebar.)
struct LaneStack<A: View, B: View, C: View, D: View, E: View>: View {
  let transcript: A, topics: B, wordgraph: C, summaries: D, nextSteps: E
  @AppStorage("live.lane.transcript") private var wTranscript = 210.0
  @AppStorage("live.lane.topics") private var wTopics = 170.0
  @AppStorage("live.lane.summaries") private var wSummaries = 190.0
  @AppStorage("live.lane.nextSteps") private var wNext = 170.0

  var body: some View {
    GeometryReader { geo in
      // A narrow window scales the four sized lanes down, so the stack never runs past
      // the window's edge and the wordgraph keeps at least 160 pt.
      let k = min(1, max(0, geo.size.width - 160 - 4) / (wTranscript + wTopics + wSummaries + wNext))
      HStack(spacing: 0) {
        transcript.frame(width: wTranscript * k)
        LaneHandle(width: $wTranscript, sign: 1)
        topics.frame(width: wTopics * k)
        LaneHandle(width: $wTopics, sign: 1)
        wordgraph.frame(minWidth: 0, maxWidth: .infinity)
        LaneHandle(width: $wSummaries, sign: -1)
        summaries.frame(width: wSummaries * k)
        LaneHandle(width: $wNext, sign: -1)
        nextSteps.frame(width: wNext * k)
      }
    }
  }
}

/// A 1 pt divider with a wider invisible grip; dragging resizes the lane on its `sign` side.
struct LaneHandle: View {
  @Binding var width: Double
  let sign: Double
  @State private var start: Double?
  var body: some View {
    Divider()
      .overlay(Color.clear.frame(width: 9).contentShape(Rectangle())
        .onHover { $0 ? NSCursor.resizeLeftRight.push() : NSCursor.pop() }
        .gesture(DragGesture(minimumDistance: 1)
          .onChanged { g in
            let from = start ?? width
            start = from
            width = min(480, max(130, from + sign * g.translation.width))
          }
          .onEnded { _ in start = nil }))
  }
}

struct LiveHeader: View {
  @Environment(AppModel.self) var model
  @Environment(\.openWindow) var openWindow
  var body: some View {
    @Bindable var live = model.live
    HStack(spacing: 10) {
      if live.active {
        Circle().fill(.red).frame(width: 8, height: 8)
        Text("Live").font(.callout.weight(.semibold))
      } else if live.session != nil {
        Text(live.session?.ended == true ? "Call ended" : "Not updating").font(.callout.weight(.semibold)).foregroundStyle(.secondary)
      }
      if let s = live.session {
        if let st = s.started { Text("started \(st.formatted(date: .omitted, time: .shortened))").foregroundStyle(.secondary) }
        Text("\(s.entries.count) lines").foregroundStyle(.secondary)
        if let i = s.insights { Text("\(i.topics.count) topics").foregroundStyle(.secondary) }
      }
      Spacer()
      Toggle("Full-window wordgraph", isOn: $live.fullWordgraph).toggleStyle(.switch).controlSize(.small)
        .disabled(live.session == nil).fixedSize()
      Button { openWindow(id: "live-panel") } label: { Image(systemName: "rectangle.portrait.on.rectangle.portrait") }
        .help("Open the compact live panel (⌘L)")
    }
    .font(.callout)
    .lineLimit(1)
    .padding(.horizontal, 14).padding(.vertical, 8)
  }
}

struct LaneTitle: View {
  let title: String
  var detail: String?
  var body: some View {
    HStack {
      Text(title).font(.headline)
      Spacer()
      if let detail { Text(detail).font(.caption).foregroundStyle(.secondary) }
    }
    .padding(.horizontal, 12).padding(.vertical, 8)
  }
}

struct WhoLabel: View {
  let who: String
  var body: some View {
    Text(who).font(.caption.weight(.semibold)).foregroundStyle(who == "me" ? Color.accentColor : .orange)
      .frame(width: 34, alignment: .leading)
  }
}

struct TranscriptLane: View {
  let entries: [LiveEntry]
  let active: Bool
  var body: some View {
    VStack(spacing: 0) {
      LaneTitle(title: "Transcript", detail: "me / them")
      Divider()
      TranscriptLaneBody(entries: entries, active: active)
    }
  }
}

struct TopicsLane: View {
  let insights: LiveInsights?
  var body: some View {
    VStack(spacing: 0) {
      LaneTitle(title: "Topics", detail: insights.map { "\($0.topics.count) buckets" })
      Divider()
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 8) {
          ForEach(insights?.topics ?? []) { t in
            VStack(alignment: .leading, spacing: 4) {
              HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(t.label).font(.body.weight(.semibold)).lineLimit(2)
                Spacer(minLength: 4)
                Text("\(Int((t.share * 100).rounded()))%").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
              }
              if t.visits > 1 || t.isNew {
                HStack(spacing: 4) {
                  if t.visits > 1 { Chip(text: "back \(t.visits)×", color: .yellow) }
                  if t.isNew { Chip(text: "new", color: .green) }
                }
              }
              if !t.terms.isEmpty {
                Text(t.terms.prefix(5).joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
              }
            }
            .padding(10)
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
          }
          if insights == nil { Text("Topics appear as the call goes on").font(.caption).foregroundStyle(.secondary) }
        }
        .padding(12)
      }
    }
  }
}

struct Chip: View {
  let text: String
  let color: Color
  var body: some View {
    Text(text).font(.caption2.weight(.bold)).foregroundStyle(color)
      .padding(.horizontal, 5).padding(.vertical, 1)
      .background(color.opacity(0.15), in: Capsule())
  }
}

struct WordgraphLane: View {
  @Environment(AppModel.self) var model
  let insights: LiveInsights?
  let full: Bool
  var body: some View {
    VStack(spacing: 0) {
      LaneTitle(title: "Wordgraph", detail: full ? "drag · throw · click" : "topics and ideas")
      Divider()
      WordgraphView(insightsJSON: insights?.json, motion: full ? "lively" : "calm", maxDepth: full ? -1 : 1)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
  }
}

struct SummariesLane: View {
  let insights: LiveInsights?
  var body: some View {
    VStack(spacing: 0) {
      LaneTitle(title: "Summaries", detail: insights?.aiModel == nil ? "every 5 min" : "AI recap + every 5 min")
      Divider()
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 10) {
          if let i = insights, !i.aiRecap.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
              Label("So far (AI)", systemImage: "sparkles").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
              ForEach(i.aiRecap, id: \.self) { b in
                Text("• " + b).fixedSize(horizontal: false, vertical: true)
              }
            }
            .padding(10)
            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
          }
          ForEach((insights?.summaries ?? []).reversed()) { s in
            VStack(alignment: .leading, spacing: 4) {
              Text("\(Transcript.clock(s.from)) to \(Transcript.clock(s.to))").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
              if s.bullets.isEmpty {
                Text(s.text).fixedSize(horizontal: false, vertical: true)
              } else {
                ForEach(s.bullets, id: \.self) { b in
                  Text("• " + b).fixedSize(horizontal: false, vertical: true)
                }
              }
            }
            .padding(10)
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
          }
        }
        .padding(12)
      }
    }
  }
}

struct NextStepsLane: View {
  let insights: LiveInsights?
  var body: some View {
    VStack(spacing: 0) {
      LaneTitle(title: "Next steps", detail: insights?.aiModel == nil ? "live" : "live · AI")
      Divider()
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 8) {
          section("Agreed", insights?.agreed ?? [], symbol: "checkmark.circle.fill", color: .green)
          section("Suggested", insights?.suggested ?? [], symbol: "lightbulb", color: .yellow)
        }
        .padding(12)
      }
    }
  }

  @ViewBuilder func section(_ title: String, _ steps: [NextStep], symbol: String, color: Color) -> some View {
    Text(title.uppercased()).font(.caption.weight(.bold)).foregroundStyle(.secondary).padding(.top, 4)
    if steps.isEmpty { Text("None yet").font(.caption).foregroundStyle(.tertiary) }
    ForEach(steps) { n in
      HStack(alignment: .top, spacing: 6) {
        Image(systemName: symbol).foregroundStyle(color)
        VStack(alignment: .leading, spacing: 2) {
          Text((n.who == "me" ? "You: " : "") + n.text).fixedSize(horizontal: false, vertical: true)
          Text(n.clock).font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
        }
      }
    }
  }
}

/// ⌘L: a narrow floating strip to pin next to the call: the transcript tail, topic chips,
/// and "ask about this call".
struct LivePanel: View {
  @Environment(AppModel.self) var model
  @State private var question = ""

  var body: some View {
    let live = model.live
    VStack(alignment: .leading, spacing: 0) {
      HStack(spacing: 6) {
        if live.active { Circle().fill(.red).frame(width: 7, height: 7) }
        Text(live.active ? "Live" : (live.session == nil ? "No live call" : "Call ended")).font(.headline)
        Spacer()
        if let n = live.session?.entries.count { Text("\(n) lines").font(.caption).foregroundStyle(.secondary) }
      }
      .padding(10)
      if let topics = live.session?.insights?.topics, !topics.isEmpty {
        ScrollView(.horizontal, showsIndicators: false) {
          HStack(spacing: 6) {
            ForEach(topics.prefix(6)) { t in
              Text(t.label + (t.visits > 1 ? " \(t.visits)×" : "")).font(.caption)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(.quaternary, in: Capsule())
            }
          }
          .padding(.horizontal, 10)
        }
        .padding(.bottom, 6)
      }
      Divider()
      TranscriptLaneBody(entries: Array((live.session?.entries ?? []).suffix(40)), active: live.active)
      Divider()
      VStack(alignment: .leading, spacing: 6) {
        if let a = live.answer {
          // A search answer is just its lines, listed below; only an engine adds prose.
          if a.mode != "search", !a.text.isEmpty { Text(a.text).font(.callout).fixedSize(horizontal: false, vertical: true) }
          ForEach(a.matches) { e in
            HStack(alignment: .top, spacing: 4) {
              Text(e.clock).font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
              WhoLabel(who: e.who)
              Text(e.text).font(.caption).fixedSize(horizontal: false, vertical: true)
            }
          }
          Text(a.mode == "claude" ? "Answered by the configured engine from the live transcript"
               : "From the live transcript (search; no engine configured)")
            .font(.caption2).foregroundStyle(.tertiary)
        }
        HStack {
          TextField("Ask about this call", text: $question).textFieldStyle(.roundedBorder)
            .onSubmit { Task { await live.ask(question, workspace: model.paths.workspace) } }
          if live.asking { ProgressView().controlSize(.small) }
        }
      }
      .padding(10)
    }
    .frame(minWidth: 300, idealWidth: 340, minHeight: 420)
    .onAppear { live.panelOpen = true }
    .onDisappear { live.panelOpen = false }
  }
}

/// The transcript lines without the lane title, for the panel.
struct TranscriptLaneBody: View {
  let entries: [LiveEntry]
  let active: Bool
  var body: some View {
    ScrollViewReader { proxy in
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 5) {
          ForEach(entries) { e in
            VStack(alignment: .leading, spacing: 1) {
              HStack(spacing: 4) {
                WhoLabel(who: e.who)
                Text(e.clock).font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
              }
              Text(e.text).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
            .id(e.id)
          }
          if active { Text("… listening").font(.caption).foregroundStyle(.secondary).id("tail") }
        }
        .padding(10)
      }
      .onChange(of: entries.last?.id, initial: true) { proxy.scrollTo(active ? "tail" : entries.last?.id, anchor: .bottom) }
    }
  }
}
