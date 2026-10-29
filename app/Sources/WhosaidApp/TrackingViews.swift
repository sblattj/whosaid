import SwiftUI
import WhosaidKit

/// My commitments: the ranked worklist for the workspace owner, with suggested
/// completions on top. Nothing changes until a Mark done button is clicked.
struct CommitmentsView: View {
  @Environment(AppModel.self) var model
  @State private var filter: ItemFilter = .open

  var rows: [TrackedItem] { model.worklist.filter(filter.keeps) }

  var body: some View {
    VStack(alignment: .leading, spacing: 0) {
      FilterBar(filter: $filter, counts: counts(model.worklist))
      Divider()
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 10) {
          if let err = model.trackingError { Text(err).foregroundStyle(.orange) }
          let pending = model.suggestions.filter { s in model.worklist.contains { $0.id == s.itemId && !$0.isDone } }
          if filter != .done, !pending.isEmpty {
            Text("Looks done? Review these").font(.headline)
            ForEach(pending) { s in SuggestionCard(suggestion: s, item: model.worklist.first { $0.id == s.itemId }) }
            Divider().padding(.vertical, 4)
          }
          ForEach(rows) { TrackedRow(item: $0) }
          if rows.isEmpty {
            Text(filter == .done ? "Nothing marked done yet" : "Nothing open. Commitments appear after ingest.")
              .foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.top, 40)
          }
        }
        .padding(16)
      }
    }
    .task { await model.loadWorklist() }
  }
}

/// Every action item in the workspace, any owner.
struct ActionItemsView: View {
  @Environment(AppModel.self) var model
  @State private var filter: ItemFilter = .open

  var body: some View {
    VStack(alignment: .leading, spacing: 0) {
      FilterBar(filter: $filter, counts: counts(model.items))
      Divider()
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 10) {
          ForEach(model.items.filter(filter.keeps)) { TrackedRow(item: $0, showOwner: true) }
          if model.items.filter(filter.keeps).isEmpty {
            Text("No action items here").foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.top, 40)
          }
        }
        .padding(16)
      }
    }
    .task { await model.loadItems() }
  }
}

func counts(_ items: [TrackedItem]) -> [ItemFilter: Int] {
  Dictionary(uniqueKeysWithValues: ItemFilter.allCases.map { f in (f, items.filter(f.keeps).count) })
}

struct FilterBar: View {
  @Binding var filter: ItemFilter
  let counts: [ItemFilter: Int]
  var body: some View {
    Picker("", selection: $filter) {
      ForEach(ItemFilter.allCases) { f in Text("\(f.rawValue) \(counts[f] ?? 0)").tag(f) }
    }
    .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 320).padding(12)
  }
}

struct TierChip: View {
  let tier: String
  var color: Color { tier == "P1" ? .red : tier == "P2" ? .orange : .secondary }
  var body: some View {
    Text(tier).font(.caption.weight(.bold)).foregroundStyle(color)
      .padding(.horizontal, 6).padding(.vertical, 2)
      .background(color.opacity(0.15), in: Capsule())
  }
}

/// One commitment or action item: tier, text, why, where it was said, and Mark done.
struct TrackedRow: View {
  @Environment(AppModel.self) var model
  let item: TrackedItem
  var showOwner = false
  @State private var confirming = false
  @State private var error: String?

  var body: some View {
    HStack(alignment: .top, spacing: 10) {
      Image(systemName: item.isDone ? "checkmark.circle.fill" : "circle")
        .foregroundStyle(item.isDone ? .green : .secondary).padding(.top, 2)
      VStack(alignment: .leading, spacing: 4) {
        HStack(spacing: 6) {
          if let tier = item.tier { TierChip(tier: tier) }
          Text(item.text).strikethrough(item.isDone).fixedSize(horizontal: false, vertical: true)
        }
        HStack(spacing: 8) {
          Text(item.id).font(.caption.monospaced())
          if showOwner, !item.owner.isEmpty { Text(item.owner).foregroundStyle(SpeakerColor.of(item.owner)) }
          if let k = item.kindLabel { Text(k) }
          if let s = item.section { Text(s) }
          if item.status != "open" { Text(item.status) }
          if let m = item.meeting {
            Button { model.openSource(meeting: m, t: item.t) } label: {
              Label(m + (item.t.map { " " + Transcript.clock($0) } ?? ""), systemImage: "arrow.up.forward.square")
            }
            .buttonStyle(.link).help("Open where this was said")
          }
        }
        .font(.caption).foregroundStyle(.secondary)
        if !item.why.isEmpty {
          Text(item.why.joined(separator: " · ")).font(.caption).foregroundStyle(.tertiary)
        }
        if let error { Text(error).font(.caption).foregroundStyle(.orange) }
      }
      Spacer()
      if !item.isDone {
        Button("Mark done") { confirming = true }
          .confirmationDialog("Mark \(item.id) done?", isPresented: $confirming) {
            Button("Mark done") {
              Task { error = await model.markDone(item.id, note: "Marked done in the whosaid app.") }
            }
          } message: { Text(item.text) }
      }
    }
    .padding(10)
    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
  }
}

/// A later statement that may show an item is done. Accepting it records the quote as evidence.
struct SuggestionCard: View {
  @Environment(AppModel.self) var model
  let suggestion: CompletionSuggestion
  let item: TrackedItem?
  @State private var working = false
  @State private var error: String?

  var body: some View {
    VStack(alignment: .leading, spacing: 6) {
      HStack(spacing: 6) {
        Image(systemName: "sparkles").foregroundStyle(.yellow)
        Text(item?.text ?? suggestion.itemId).font(.body.weight(.medium))
        Text(suggestion.itemId).font(.caption.monospaced()).foregroundStyle(.secondary)
      }
      HStack(alignment: .top, spacing: 6) {
        Text("“\(suggestion.quote)”").italic()
        if let sp = suggestion.speaker { Text("– " + sp).foregroundStyle(SpeakerColor.of(sp)) }
      }
      .font(.callout)
      Text(suggestion.reason).font(.caption).foregroundStyle(.secondary)
      HStack {
        if let m = suggestion.meeting {
          Button { model.openSource(meeting: m, t: suggestion.t) } label: {
            Label("Open " + m + (suggestion.t.map { " " + Transcript.clock($0) } ?? ""), systemImage: "arrow.up.forward.square")
          }
          .buttonStyle(.link).font(.caption)
        }
        Spacer()
        if working { ProgressView().controlSize(.small) }
        Button("Mark done") {
          working = true
          Task {
            error = await model.markDone(suggestion.itemId, note: suggestion.note)
            working = false
          }
        }
        .buttonStyle(.borderedProminent).disabled(working)
      }
      if let error { Text(error).font(.caption).foregroundStyle(.orange) }
    }
    .padding(12)
    .background(Color.yellow.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.yellow.opacity(0.3)))
  }
}
