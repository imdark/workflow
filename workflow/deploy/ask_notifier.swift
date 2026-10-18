// One question from an agent run, as a macOS notification you can answer.
//
//     open -n -W --stdout <out> "NotesGraph Agent.app" --args '<ask json>'
//
// Built and started by agent_mcp.py (see `MacQuestion`); it has to go
// through `open`, as macOS only lets an app LaunchServices started post
// notifications. Shows the question as a notification; a click opens a
// dialog with the whole question -- the tool input for a permission (a Bash
// call as its description and its command, see `ToolInput`), the
// choices for a question, a box to type in -- like the phone's notification
// pane. The notification itself has quick Allow / Deny or Reply buttons.
//
// The ask json: {"id", "kind": "question" | "permission", "heading",
// "title", "text", "detail", "options", "dir", "parentPid"}.
//
// Prints one JSON line on stdout and exits:
//     {"shown": false, "error": ...}     notifications are off; nothing shown
//     {"answer": ...}                    a question's answer
//     {"allowed": true | false, "answer"?: ...}   a permission's
// It quits quietly, taking the notification away, once `<dir>/<id>.cancel`
// appears (answered elsewhere) or parentPid is gone.
//
// Every question has its own process, but macOS hands a click to whichever
// one it likes; one that gets another's click drops it in `<dir>/<id>.click`
// for the owner to pick up.

import AppKit
import UserNotifications

struct Ask: Decodable {
  let id: String
  let kind: String
  let heading: String
  let title: String
  let text: String
  let detail: String?
  let options: [String]
  let dir: String
  let parentPid: Int32?
}

struct Click: Codable {
  let action: String
  let text: String?
}

let openAction = UNNotificationDefaultActionIdentifier
let allowAction = "allow"
let denyAction = "deny"
let replyAction = "reply"

final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
  let ask: Ask
  let center = UNUserNotificationCenter.current()
  var dialogOpen = false

  init(ask: Ask) {
    self.ask = ask
  }

  func file(_ id: String, _ suffix: String) -> URL {
    URL(fileURLWithPath: ask.dir).appendingPathComponent("\(id).\(suffix)")
  }

  var parentGone: Bool {
    guard let pid = ask.parentPid else { return false }
    return kill(pid, 0) != 0 && errno == ESRCH
  }

  func applicationDidFinishLaunching(_ notification: Notification) {
    center.delegate = self
    center.setNotificationCategories([
      UNNotificationCategory(
        identifier: "permission",
        actions: [
          UNNotificationAction(identifier: allowAction, title: "Allow"),
          UNNotificationAction(identifier: denyAction, title: "Deny"),
        ],
        intentIdentifiers: []),
      UNNotificationCategory(
        identifier: "question",
        actions: [
          UNTextInputNotificationAction(
            identifier: replyAction, title: "Reply", options: [],
            textInputButtonTitle: "Send", textInputPlaceholder: "Your answer"),
        ],
        intentIdentifiers: []),
    ])
    center.requestAuthorization(options: [.alert, .sound]) { granted, error in
      DispatchQueue.main.async {
        guard granted else {
          self.finish(["shown": false, "error": error?.localizedDescription ?? "notifications are off"])
          return
        }
        self.post(sound: true)
      }
    }
    // Answered elsewhere, the run gone, or a click macOS gave to another
    // question's process. Common modes, so it runs while a dialog is up too.
    let watch = Timer(timeInterval: 0.5, repeats: true) { _ in
      let cancel = self.file(self.ask.id, "cancel")
      if FileManager.default.fileExists(atPath: cancel.path) || self.parentGone {
        try? FileManager.default.removeItem(at: cancel)
        self.quit()
      }
      let clickFile = self.file(self.ask.id, "click")
      guard let data = try? Data(contentsOf: clickFile) else { return }
      try? FileManager.default.removeItem(at: clickFile)
      if let click = try? JSONDecoder().decode(Click.self, from: data) {
        self.handle(click)
      }
    }
    RunLoop.main.add(watch, forMode: .common)
  }

  func post(sound: Bool) {
    let content = UNMutableNotificationContent()
    content.title = ask.heading
    content.subtitle = ask.title
    var body = ask.text
    // "Allow Bash?" alone says little: add what it is doing, or the command.
    if ask.kind == "permission", let input = ask.detail.flatMap(ToolInput.init),
      let doing = input.description ?? input.command
    {
      body += "\n" + doing
    }
    content.body = String(body.prefix(300))
    content.categoryIdentifier = ask.kind == "permission" ? "permission" : "question"
    if sound {
      content.sound = UNNotificationSound(named: UNNotificationSoundName("Glass"))
    }
    center.add(UNNotificationRequest(identifier: ask.id, content: content, trigger: nil)) { error in
      if let error = error {
        DispatchQueue.main.async {
          self.finish(["shown": false, "error": error.localizedDescription])
        }
      }
    }
  }

  // Show it even though this app counts as frontmost while a dialog is up.
  func userNotificationCenter(
    _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
  ) {
    completionHandler([.banner, .list, .sound])
  }

  func userNotificationCenter(
    _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
    withCompletionHandler completionHandler: @escaping () -> Void
  ) {
    let click = Click(
      action: response.actionIdentifier,
      text: (response as? UNTextInputNotificationResponse)?.userText)
    let id = response.notification.request.identifier
    if id == ask.id {
      // After the handler returns: the dialog is modal.
      DispatchQueue.main.async { self.handle(click) }
    } else if let data = try? JSONEncoder().encode(click) {
      try? data.write(to: file(id, "click"), options: .atomic)
    }
    completionHandler()
  }

  func handle(_ click: Click) {
    switch click.action {
    case allowAction:
      finish(["allowed": true])
    case denyAction:
      finish(["allowed": false])
    case replyAction:
      let text = (click.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
      if text.isEmpty {
        showDialog()
      } else {
        finish(["answer": text])
      }
    case UNNotificationDismissActionIdentifier:
      break
    default:
      showDialog()
    }
  }

  /// The whole question, with every way to answer it.
  func showDialog() {
    if dialogOpen { return }
    dialogOpen = true
    defer { dialogOpen = false }
    NSApp.activate(ignoringOtherApps: true)

    let permission = ask.kind == "permission"
    let alert = NSAlert()
    alert.messageText = ask.text
    alert.informativeText = ask.title.isEmpty ? ask.heading : "\(ask.heading) · \(ask.title)"

    let width: CGFloat = 460
    let stack = NSStackView()
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = 8

    if let detail = ask.detail, !detail.isEmpty {
      if permission, let input = ToolInput(detail) {
        input.views(width: width).forEach(stack.addArrangedSubview)
      } else {
        stack.addArrangedSubview(
          textBox(prettyDetail(detail), monospaced: true, width: width, maxHeight: 180))
      }
    }

    let field = NSTextField()
    field.placeholderString = permission ? "Note for the agent (optional)" : "Type an answer"
    field.translatesAutoresizingMaskIntoConstraints = false
    field.widthAnchor.constraint(equalToConstant: width).isActive = true
    stack.addArrangedSubview(field)

    stack.frame = NSRect(origin: .zero, size: stack.fittingSize)
    alert.accessoryView = stack

    // NSAlert numbers its buttons from the first one added.
    var choices: [String] = []
    if permission {
      alert.addButton(withTitle: "Allow")
      alert.addButton(withTitle: "Deny")
    } else {
      alert.addButton(withTitle: "Send")
      for option in ask.options {
        alert.addButton(withTitle: option)
        choices.append(option)
      }
    }
    alert.addButton(withTitle: "Later")
    alert.window.initialFirstResponder = field
    // Send waits for something typed, rather than bouncing an empty one.
    var typing: NSObjectProtocol?
    if !permission {
      let send = alert.buttons[0]
      send.isEnabled = false
      typing = NotificationCenter.default.addObserver(
        forName: NSControl.textDidChangeNotification, object: field, queue: .main
      ) { _ in
        send.isEnabled = !field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      }
    }
    defer { typing.map(NotificationCenter.default.removeObserver) }

    let index = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
    let typed = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    if permission {
      switch index {
      case 0, 1:
        var result: [String: Any] = ["allowed": index == 0]
        if !typed.isEmpty { result["answer"] = typed }
        finish(result)
      default:
        later()
      }
      return
    }
    if index == 0 && !typed.isEmpty {
      finish(["answer": typed])
    } else if index > 0 && index - 1 < choices.count {
      finish(["answer": choices[index - 1]])
    } else {
      later()
    }
  }

  /// Not now: put the notification back, quietly, for later.
  func later() {
    NSApp.hide(nil)
    post(sound: false)
  }

  func finish(_ result: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: result),
      let line = String(data: data, encoding: .utf8)
    {
      print(line)
      fflush(stdout)
    }
    quit()
  }

  func quit() {
    center.removeDeliveredNotifications(withIdentifiers: [ask.id])
    center.removePendingNotificationRequests(withIdentifiers: [ask.id])
    // Give the removal a moment to reach the notification center. Right
    // here rather than queued, as a dialog may be up.
    Thread.sleep(forTimeInterval: 0.3)
    exit(0)
  }
}

/// The tool input a permission asks about, laid out to read: the tool's own
/// account of what it is doing, the command it wants to run (Bash above all)
/// and the rest of its input as labelled fields -- the Mac's copy of
/// NotesGraph's tool-input.ts, rather than one block of JSON. nil when the
/// detail is no JSON object (clipped by the server's cap, say), which then
/// shows as it came.
struct ToolInput {
  struct Field {
    let label: String
    let value: String
    /// Gets its own box, rather than sitting beside its label.
    let block: Bool
  }

  let description: String?
  let command: String?
  let fields: [Field]

  // Longer one-line values read better in a box than squeezed beside a label.
  static let inlineMax = 60

  init?(_ detail: String) {
    guard let data = detail.data(using: .utf8),
      let record = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    else { return nil }
    let said = (record["description"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
    description = said?.isEmpty == false ? said : nil
    command = record["command"] as? String
    var fields: [Field] = []
    // JSONSerialization keeps no key order: the path and an edit's before
    // and after in the order they read, then the rest sorted.
    let leading = ["file_path", "notebook_path", "old_string", "new_string"]
    let keys = leading.filter { record[$0] != nil }
      + record.keys.filter { !leading.contains($0) }.sorted()
    for key in keys {
      if key == "description" && description != nil { continue }
      if key == "command" && command != nil { continue }
      if let field = ToolInput.field(key, record[key]!) { fields.append(field) }
    }
    self.fields = fields
  }

  static func field(_ key: String, _ raw: Any) -> Field? {
    if raw is NSNull { return nil }
    if let number = raw as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() {
      return Field(label: humanize(key), value: number.boolValue ? "yes" : "no", block: false)
    }
    if raw is [Any] || raw is [String: Any] {
      let pretty = (try? JSONSerialization.data(
        withJSONObject: raw, options: [.prettyPrinted, .withoutEscapingSlashes]))
        .flatMap { String(data: $0, encoding: .utf8) } ?? "\(raw)"
      return Field(label: humanize(key), value: pretty, block: true)
    }
    let value = "\(raw)"
    if value.isEmpty { return nil }
    return Field(
      label: humanize(key), value: value,
      block: value.contains("\n") || value.count > inlineMax)
  }

  /// `run_in_background` / `runInBackground` -> "Run in background".
  static func humanize(_ key: String) -> String {
    let words = key
      .replacingOccurrences(of: "([a-z0-9])([A-Z])", with: "$1 $2", options: .regularExpression)
      .replacingOccurrences(of: "[_-]+", with: " ", options: .regularExpression)
      .trimmingCharacters(in: .whitespaces)
      .lowercased()
    return words.prefix(1).uppercased() + words.dropFirst()
  }

  /// What it says it is doing in one box, the command in another, then the rest.
  func views(width: CGFloat) -> [NSView] {
    var views: [NSView] = []
    if let description = description {
      views.append(textBox(description, monospaced: false, width: width, maxHeight: 80))
    }
    if let command = command {
      views.append(textBox(command, monospaced: true, width: width, maxHeight: 160))
    }
    for field in fields {
      if field.block {
        views.append(label(field.label))
        views.append(textBox(field.value, monospaced: true, width: width, maxHeight: 120))
      } else {
        let line = label("\(field.label): \(field.value)")
        line.preferredMaxLayoutWidth = width
        views.append(line)
      }
    }
    return views
  }

  func label(_ text: String) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = .systemFont(ofSize: 11)
    label.textColor = .secondaryLabelColor
    return label
  }
}

/// Read-only text in a bordered box, as tall as the text up to maxHeight,
/// scrolling past that.
func textBox(_ text: String, monospaced: Bool, width: CGFloat, maxHeight: CGFloat) -> NSView {
  let scroll = NSTextView.scrollableTextView()
  scroll.frame = NSRect(x: 0, y: 0, width: width, height: maxHeight)
  scroll.borderType = .bezelBorder
  let textView = scroll.documentView as! NSTextView
  textView.isEditable = false
  textView.font = monospaced ? .monospacedSystemFont(ofSize: 11, weight: .regular) : .systemFont(ofSize: 12)
  textView.textContainerInset = NSSize(width: 2, height: 4)
  textView.string = text
  let container = textView.textContainer!
  let layout = textView.layoutManager!
  // Laid out at the width it will have, leaving room for a scroller.
  container.containerSize = NSSize(
    width: scroll.contentSize.width - NSScroller.scrollerWidth(for: .regular, scrollerStyle: .overlay)
      - 2 * textView.textContainerInset.width,
    height: .greatestFiniteMagnitude)
  layout.ensureLayout(for: container)
  let needed = layout.usedRect(for: container).height + 2 * textView.textContainerInset.height + 4
  scroll.translatesAutoresizingMaskIntoConstraints = false
  scroll.widthAnchor.constraint(equalToConstant: width).isActive = true
  scroll.heightAnchor.constraint(equalToConstant: min(max(needed, 24), maxHeight)).isActive = true
  return scroll
}

/// A tool's input arrives as compact JSON; indent it to read.
func prettyDetail(_ detail: String) -> String {
  guard let data = detail.data(using: .utf8),
    let object = try? JSONSerialization.jsonObject(with: data),
    let pretty = try? JSONSerialization.data(
      withJSONObject: object, options: [.prettyPrinted, .withoutEscapingSlashes]),
    let text = String(data: pretty, encoding: .utf8)
  else { return detail }
  return text
}

guard CommandLine.arguments.count > 1,
  let ask = try? JSONDecoder().decode(Ask.self, from: Data(CommandLine.arguments[1].utf8))
else {
  FileHandle.standardError.write("usage: ask-notifier '<ask json>'\n".data(using: .utf8)!)
  exit(2)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let notifier = Notifier(ask: ask)
app.delegate = notifier
app.run()
