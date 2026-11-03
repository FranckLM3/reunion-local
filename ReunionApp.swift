// Réunion : icône de barre des menus pour enregistrer une réunion d'un clic.
// Pilote serveur.py (lancé en coulisse) par son API locale ; propose d'enregistrer
// dès qu'une autre appli allume le micro (Meet dans Firefox, etc.).
// Mêmes règles que la page : un bouton, le nom en option, aucune case d'expert (spec BM-01 à BM-12).
// Construction : ./build_app.sh

import AppKit
import CoreAudio
import ServiceManagement
import SwiftUI

let PORT = 8765
let BASE_URL = URL(string: "http://127.0.0.1:\(PORT)")!
let DOSSIER = Bundle.main.object(forInfoDictionaryKey: "ReunionDossier") as? String ?? ""

func hms(_ s: Double) -> String {
    let t = max(0, Int(s))
    return t >= 3600
        ? String(format: "%d:%02d:%02d", t / 3600, t % 3600 / 60, t % 60)
        : String(format: "%02d:%02d", t / 60, t % 60)
}

func hhmm(_ epoch: Double) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    return f.string(from: Date(timeIntervalSince1970: epoch))
}

/// Niveau en un mot, jamais en dB (même échelle que la page).
func mot(_ db: Double?) -> String {
    guard let db, db >= -60 else { return "Rien" }
    return db < -40 ? "Faible" : db < -12 ? "Bon" : "Fort"
}

// MARK: - API locale

struct Erreur: Decodable { let code: String; let cause: String }

struct Alerte: Decodable {
    let code: String
    let niveau: String
    let minutes: Int?
    let heure: String?

    /// Même phrase que la page (BM-07).
    var texte: String {
        switch code {
        case "fige": return "L'enregistrement est bloqué depuis 30 s. Ce qui a été enregistré jusqu'ici est gardé."
        case "arret": return "L'enregistrement s'est arrêté à \(heure ?? "") : l'enregistreur a cessé de répondre. Les \(minutes ?? 0) min enregistrées sont gardées."
        case "micro_muet": return "Votre micro ne capte rien depuis 30 s. Vérifiez qu'il n'est pas coupé dans la visio ou sur l'ordinateur."
        case "son_coupe": return "Le son de la visio n'arrive plus depuis 1 min. Vérifiez la sortie audio de l'ordinateur (casque, haut-parleurs)."
        case "place": return "Il reste de la place pour environ \(minutes ?? 0) min d'enregistrement."
        case "son_jamais": return "Aucun son de l'ordinateur depuis 1 min. En visio, vérifiez que vous entendez les autres sur cet ordinateur."
        case "appareil": return "Changement d'appareil audio : l'enregistrement a suivi."
        default: return ""
        }
    }
}

struct Etat: Decodable {
    struct Enr: Decodable {
        let nom: String
        let titre: String
        let depuis: Double
        let niveau_micro: Double?
        let niveau_systeme: Double?
        let alertes: [Alerte]
    }
    struct Travail: Decodable { let en_cours: Bool; let fraction: Double?; let fin_estimee: Double? }
    let enregistrement: Enr?
    let travaux: [String: Travail]
    let nouvelles: Int?
}

struct Reponse: Decodable { let erreur: Erreur?; let nom: String? }

/// Ouvre la rubrique des Réglages Système où autoriser Réunion.
func ouvrirReglages(_ rubrique: String) {
    NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?\(rubrique)")!)
}

/// Jeton de session écrit par serveur.py à chaque lancement (~/.config/reunion/session, lisible par toi seul).
func jetonSession() -> String? {
    let f = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/reunion/session")
    return (try? String(contentsOf: f, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
}

func appel<T: Decodable>(_ chemin: String, corps: [String: Any]? = nil) async throws -> T {
    var r = URLRequest(url: BASE_URL.appendingPathComponent(chemin))
    r.timeoutInterval = 40
    if let j = jetonSession() { r.setValue("reunion=\(j)", forHTTPHeaderField: "Cookie") }
    if let corps {
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONSerialization.data(withJSONObject: corps)
    }
    let (data, _) = try await URLSession.shared.data(for: r)
    return try JSONDecoder().decode(T.self, from: data)
}

// MARK: - Micro utilisé par une autre appli

/// Vrai si une appli quelconque capte le micro par défaut (CoreAudio, sans autorisation requise).
func microActif() -> Bool {
    var id = AudioObjectID(0)
    var taille = UInt32(MemoryLayout<AudioObjectID>.size)
    var adr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &adr, 0, nil, &taille, &id) == noErr
    else { return false }
    var actif: UInt32 = 0
    taille = UInt32(MemoryLayout<UInt32>.size)
    adr.mSelector = kAudioDevicePropertyDeviceIsRunningSomewhere
    guard AudioObjectGetPropertyData(id, &adr, 0, nil, &taille, &actif) == noErr else { return false }
    return actif != 0
}

// MARK: - Modèle

@MainActor
final class Modele: ObservableObject {
    @Published var etat: Etat?
    @Published var erreur: Erreur?
    @Published var occupe = false
    @Published var maintenant = Date()
    @Published var nom = ""
    @Published var participants = ""
    @Published var note = ""
    @Published var nommerMenu = false  // « Donner un nom » replié par défaut (pas de @State : la macro manque aux outils en ligne de commande)
    @Published var nommerProposition = false
    @Published var auDemarrage = SMAppService.mainApp.status == .enabled
    var debutLocal: Date?
    private var instantNote: Double?
    private var serveur: Process?
    private var microAvant = false
    let proposition = Proposition()

    var enregistre: Bool { etat?.enregistrement != nil }
    var chrono: String { debutLocal.map { hms(maintenant.timeIntervalSince($0)) } ?? "" }
    var alerte: Alerte? { etat?.enregistrement?.alertes.first }
    var travailEnCours: (String, Etat.Travail)? {
        etat?.travaux.first { $0.value.en_cours }.map { ($0.key, $0.value) }
    }

    init() {
        lancerServeur()
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { _ in MainActor.assumeIsolated { self.quitter() } }
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            Task { @MainActor in await self.tic() }
        }
    }

    /// Démarre serveur.py s'il ne tourne pas déjà (lancé depuis un terminal, par exemple).
    func lancerServeur() {
        Task {
            if (try? await appel("api/etat") as Etat) != nil { return }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: DOSSIER + "/.venv/bin/python")
            p.arguments = [DOSSIER + "/serveur.py"]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"  // ffmpeg, invisible depuis une app
            env["REUNION_SANS_NAVIGATEUR"] = "1"
            p.environment = env
            p.standardOutput = FileHandle.nullDevice
            do {
                try p.run()
                serveur = p
            } catch {
                erreur = Erreur(code: "installation", cause: "Réunion est incomplète : une partie de l'installation manque. Réinstallez Réunion : vos réunions et vos réglages sont gardés.")
            }
        }
    }

    func quitter() {
        if let s = serveur, s.isRunning {
            s.terminate()  // SIGTERM : serveur.py finalise un enregistrement en cours
            s.waitUntilExit()
        }
    }

    private func tic() async {
        maintenant = Date()
        guard let e: Etat = try? await appel("api/etat") else { return }
        if let enr = e.enregistrement, debutLocal == nil {
            debutLocal = Date().addingTimeInterval(-enr.depuis)
        } else if e.enregistrement == nil {
            debutLocal = nil
        }
        etat = e
        surveillerMicro()
    }

    /// Propose d'enregistrer quand le micro s'allume ailleurs (front montant seulement : après « Pas cette fois »,
    /// la fenêtre ne revient pas tant que le micro reste allumé, BM-12).
    private func surveillerMicro() {
        let actif = microActif()
        defer { microAvant = actif || enregistre }  // pendant l'enregistrement, c'est meetrec qui tient le micro
        if actif, !microAvant, !enregistre, !occupe {
            proposition.montrer(modele: self)
        }
    }

    func demarrer() async {
        occupe = true
        erreur = nil
        defer { occupe = false }
        do {
            let r: Reponse = try await appel("api/demarrer", corps: ["nom": nom, "participants": participants])
            erreur = r.erreur
            if r.erreur == nil {
                nom = ""
                participants = ""
                debutLocal = Date()
                proposition.fermer()
            }
        } catch {
            erreur = Erreur(code: "hors_ligne", cause: "Réunion ne répond plus. Quittez puis rouvrez Réunion.")
        }
        await tic()
    }

    /// Note : l'instant de la première frappe (NT-01 à NT-04).
    func noteModifiee() {
        if note.trimmingCharacters(in: .whitespaces).isEmpty { instantNote = nil }
        else if instantNote == nil, let d = debutLocal { instantNote = Date().timeIntervalSince(d) }
    }

    func ajouterNote() async {
        let texte = note.trimmingCharacters(in: .whitespaces)
        guard !texte.isEmpty, let enr = etat?.enregistrement else { note = ""; instantNote = nil; return }
        let t = instantNote ?? debutLocal.map { Date().timeIntervalSince($0) } ?? 0
        note = ""
        instantNote = nil
        let _: Reponse? = try? await appel("api/note", corps: ["reunion": enr.nom, "action": "ajouter",
                                                               "t_s": (t * 10).rounded() / 10, "texte": texte])
    }

    func arreter() async {
        occupe = true
        defer { occupe = false }
        if !note.trimmingCharacters(in: .whitespaces).isEmpty { await ajouterNote() }  // NT-06
        let r: Reponse? = try? await appel("api/arreter", corps: [:])
        erreur = r?.erreur
        await tic()
    }

    func ouvrirPage(_ ancre: String = "") {
        // Avec le jeton de session : le serveur le change en cookie (S2).
        let cle = jetonSession().map { "?cle=\($0)" } ?? ""
        NSWorkspace.shared.open(URL(string: BASE_URL.absoluteString + "/" + cle + ancre)!)
    }
}

// MARK: - Fenêtre « Enregistrer cette réunion ? »

@MainActor
final class Proposition {
    private var panneau: NSPanel?
    private var minuterie: Timer?

    func montrer(modele: Modele) {
        guard panneau == nil else { return }
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 320, height: 170),
                        styleMask: [.titled, .nonactivatingPanel, .fullSizeContentView],
                        backing: .buffered, defer: false)
        p.titlebarAppearsTransparent = true
        p.titleVisibility = .hidden
        p.level = .floating
        p.isMovableByWindowBackground = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.contentView = NSHostingView(rootView: VueProposition(modele: modele, fermer: { [weak self] in self?.fermer() }))
        if let ecran = NSScreen.main?.visibleFrame {
            p.setFrameTopLeftPoint(NSPoint(x: ecran.maxX - 340, y: ecran.maxY - 12))
        }
        p.orderFrontRegardless()  // au-dessus des autres, sans prendre le focus (BM-10)
        panneau = p
        // Sans réponse, la proposition s'efface : on peut toujours démarrer depuis la barre des menus (BM-11).
        minuterie = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { _ in
            Task { @MainActor in self.fermer() }
        }
    }

    func fermer() {
        minuterie?.invalidate()
        panneau?.close()
        panneau = nil
    }
}

struct VueProposition: View {
    @ObservedObject var modele: Modele
    let fermer: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Le micro vient de s'allumer", systemImage: "mic.fill").font(.headline)
            Text("Enregistrer cette réunion ?")
            DisclosureGroup("Donner un nom", isExpanded: $modele.nommerProposition) {
                TextField("Nom de la réunion", text: $modele.nom)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await modele.demarrer() } }
            }
            .font(.callout)
            HStack {
                Spacer()
                Button("Pas cette fois", action: fermer)
                Button("Enregistrer") { Task { await modele.demarrer() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(modele.occupe)
            }
            Text("Prévenez les participants que vous enregistrez.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(14)
        .frame(width: 320)
    }
}

// MARK: - Menu de la barre

struct VueMenu: View {
    @ObservedObject var modele: Modele

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let enr = modele.etat?.enregistrement {
                enCours(enr)
            } else {
                auRepos
            }
            if let e = modele.erreur { refus(e) }
            Divider()
            Button {
                modele.ouvrirPage()
            } label: {
                HStack(spacing: 4) {
                    Text("Ouvrir mes réunions").bold()
                    if let n = modele.etat?.nouvelles, n > 0 {
                        Text("- \(n) \(n > 1 ? "nouvelles" : "nouvelle")").foregroundStyle(.secondary)
                    }
                }
            }
            Divider()
            if !modele.enregistre {
                Button("Réglages...") { modele.ouvrirPage("#reglages") }
                Toggle("Lancer Réunion à l'ouverture de session", isOn: $modele.auDemarrage)
                    .onChange(of: modele.auDemarrage) { _, oui in
                        do {
                            try oui ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
                        } catch {
                            modele.auDemarrage = SMAppService.mainApp.status == .enabled
                        }
                    }
            }
            Button("Quitter Réunion") { NSApp.terminate(nil) }
            Divider()
            Label("Tout reste sur ce Mac.", systemImage: "lock").font(.caption).foregroundStyle(.secondary)
        }
        .buttonStyle(.borderless)
        .padding(14)
        .frame(width: 300)
    }

    /// BM-01 à BM-03, BM-08.
    private var auRepos: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                Task { await modele.demarrer() }
            } label: {
                Label("Enregistrer", systemImage: "record.circle").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
            .controlSize(.large)
            .keyboardShortcut(.defaultAction)
            .disabled(modele.occupe)
            DisclosureGroup("Donner un nom (facultatif)", isExpanded: $modele.nommerMenu) {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Nom de la réunion", text: $modele.nom).textFieldStyle(.roundedBorder)
                    TextField("Participants", text: $modele.participants).textFieldStyle(.roundedBorder)
                    Text("Prénoms séparés par des virgules. Ils servent à proposer des noms pour les voix.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .padding(.top, 4)
            }
            .font(.callout)
            if let (nom, t) = modele.travailEnCours {
                let titre = modele.titreDe(nom)
                Text("Transcription de « \(titre) » : \(Int((t.fraction ?? 0) * 100)) %"
                     + (t.fin_estimee.map { ", prête vers \(hhmm($0))" } ?? ""))
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// BM-06, BM-07.
    private func enCours(_ enr: Etat.Enr) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let a = modele.alerte {
                Label(a.texte, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(a.niveau == "rouge" ? .red : .orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                Circle().fill(.red).frame(width: 12, height: 12)
                Text(modele.chrono).font(.system(size: 28, weight: .semibold).monospacedDigit())
            }
            Text(enr.titre)
            Text("Votre micro : \(mot(enr.niveau_micro)) - Son de l'ordinateur : \(mot(enr.niveau_systeme))")
                .font(.caption)
            TextField("Ajouter une note", text: $modele.note)
                .textFieldStyle(.roundedBorder)
                .onChange(of: modele.note) { _, _ in modele.noteModifiee() }
                .onSubmit { Task { await modele.ajouterNote() } }
            Button {
                Task { await modele.arreter() }
            } label: {
                Label("Arrêter", systemImage: "stop.fill").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
            .controlSize(.large)
            .disabled(modele.occupe)
            Button("Voir le direct") { modele.ouvrirPage("#enreg") }
                .foregroundStyle(Color.accentColor)
        }
    }

    /// BM-09 : une phrase, un seul bouton vers la bonne rubrique.
    private func refus(_ e: Erreur) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(e.cause).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            if e.code == "micro_refuse" || e.code == "son_refuse" {
                Button("Ouvrir les réglages du Mac") {
                    ouvrirReglages(e.code == "micro_refuse" ? "Privacy_Microphone" : "Privacy_ScreenCapture")
                }
                .buttonStyle(.bordered)
                Text("Cochez Réunion, puis quittez et rouvrez Réunion.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

extension Modele {
    /// « 261103-1305-point-devis » -> « point devis » (le titre exact vient de la page).
    func titreDe(_ nom: String) -> String {
        let parties = nom.split(separator: "-", maxSplits: 2)
        return parties.count == 3 ? parties[2].replacingOccurrences(of: "-", with: " ") : nom
    }
}

@main
struct ReunionApp: App {
    @StateObject private var modele = Modele()

    var body: some Scene {
        MenuBarExtra {
            VueMenu(modele: modele)
        } label: {
            if modele.enregistre {
                // Point rouge et chronomètre ; triangle d'alerte devant le chronomètre (BM-06, BM-07).
                Text("\(modele.alerte != nil ? "⚠" : "●") \(modele.chrono)").monospacedDigit()
            } else {
                Image(systemName: "waveform")
            }
        }
        .menuBarExtraStyle(.window)
    }
}
