require 'xcodeproj'
require 'json'
require 'pathname'
require 'open3'

mode, root, output, config_path, exclusion_json = ARGV
root = Pathname.new(root)
pods = root.join('Pods')
if mode == 'inspect'
  project = Xcodeproj::Project.open(pods.join('Pods.xcodeproj'))
  targets = project.targets.map do |target|
    source_phase = target.respond_to?(:source_build_phase) ? target.source_build_phase : nil
    {name: target.name, source_count: source_phase ? source_phase.files.size : 0,
     phases: target.shell_script_build_phases.map do |phase|
       {name: phase.name, script: phase.shell_script, inputs: phase.input_paths,
        outputs: phase.output_paths, input_lists: phase.input_file_list_paths,
        output_lists: phase.output_file_list_paths}
     end}
  end
  puts JSON.generate(targets: targets)
  exit
end
if mode == 'vendors'
  cfg = JSON.parse(File.read(config_path))
  excluded = JSON.parse(exclusion_json || '[]').map { |part| pods.join(part).cleanpath.to_s + '/' }
  candidates_for = lambda do |extension|
    Dir.glob(pods.join('**', "*.#{extension}")).reject do |entry|
      excluded.any? { |prefix| entry.start_with?(prefix) }
    end
  end
  architecture_list = lambda do |binary|
    data, status = Open3.capture2('xcrun', 'lipo', '-archs', binary.to_s, err: File::NULL)
    status.success? ? data.split : []
  end
  executable_path = lambda do |bundle|
    bundle = Pathname.new(bundle)
    plist = [bundle.join('Info.plist'), bundle.join('Resources/Info.plist')].find(&:file?)
    metadata = {}
    if plist
      data, status = Open3.capture2('plutil', '-convert', 'json', '-o', '-', plist.to_s)
      raise "invalid framework metadata: #{plist}" unless status.success?
      metadata = JSON.parse(data)
    end
    names = [metadata['CFBundleExecutable'], bundle.basename('.framework').to_s].compact.uniq
    names.map { |name| bundle.join(name) }.find(&:file?) || bundle.join(names.last)
  end
  choose_source = lambda do |entries, &binary_for|
    valid = entries.select { |entry| File.file?(binary_for.call(entry)) }
    valid.find { |entry| architecture_list.call(binary_for.call(entry)).include?('arm64') } || valid.first
  end
  framework_index = candidates_for.call('framework').group_by { |entry| File.basename(entry, '.framework') }
  library_index = candidates_for.call('a').group_by { |entry| File.basename(entry, '.a').delete_prefix('lib') }
  rows = cfg.fetch('framework_names').map do |name|
    source = choose_source.call(framework_index.fetch(name, []), &executable_path)
    {name: name, kind: 'framework', source: source}
  end
  rows += cfg.fetch('static_library_names').map do |name|
    source = choose_source.call(library_index.fetch(name, [])) { |entry| entry }
    {name: name, kind: 'library', source: source}
  end
  puts JSON.generate(rows)
  exit
end

cfg = JSON.parse(File.read(config_path))
overlay = Pathname.new(output)
project = Xcodeproj::Project.open(overlay.join('Pods.xcodeproj'))
# Resolve original-relative source paths before changing any reference, while
# keeping generated support and converted vendor references inside the overlay
original_project = Xcodeproj::Project.open(pods.join('Pods.xcodeproj'))
original_paths = original_project.files.to_h { |r| [r.uuid, r.real_path.to_s] }
# Development Pod paths climb out of the original Pods directory, so copying
# generated configuration one level deeper must preserve their original base
overlay.join('Target Support Files').glob('**/*').each do |file|
  next unless file.file? && %w[.xcconfig .sh .xcfilelist].include?(file.extname)
  text = file.read
  adapted = text.gsub(/\$\{PODS_ROOT\}(?=\/\.\.\/)/, pods.to_s).gsub(/\$\(PODS_ROOT\)(?=\/\.\.\/)/, pods.to_s)
  file.write(adapted) unless text == adapted
end
project.files.each do |ref|
  path = original_paths.fetch(ref.uuid)
  generated_paths = ['Target Support Files', *cfg.fetch('generated_pods_paths', [])]
  if generated_paths.any? { |name| path.start_with?(pods.join(name).to_s + '/') }
    path = path.sub(pods.to_s, overlay.to_s)
  end
  next if ref.source_tree == 'BUILT_PRODUCTS_DIR' || !Pathname.new(path).absolute?
  ref.path = path
  ref.source_tree = '<absolute>'
end

result = []
cfg.fetch('source_fallbacks').each do |name|
  target = project.targets.find { |t| t.name == name }
  raise "missing source target #{name}" unless target
  prefix = pods.join(name).to_s + '/'
  sources = original_project.files.select do |ref|
    path = original_paths.fetch(ref.uuid)
    path.start_with?(prefix) && %w[.m .mm .c .cc .cpp .cxx .swift .S .s].include?(File.extname(path)) && !path.match?(/\.(framework|bundle)\//)
  end
  raise "no source references #{name}" if sources.empty?
  sources.each { |ref| raise "missing source #{name}: #{ref.path}" unless File.file?(original_paths.fetch(ref.uuid)) }
  cache_phases = target.shell_script_build_phases.select { |phase| cfg.fetch('cache_phase_names').include?(phase.name) }
  raise "no configured cache phase found for #{name}; recheck build mechanism" if cache_phases.empty?
  cache_phases.each(&:remove_from_project)
  sources.each { |ref| target.source_build_phase.add_file_reference(project.objects_by_uuid.fetch(ref.uuid), true) }
  overlay.join('Target Support Files', name).glob('*.modulemap').each do |map|
    text = map.read.gsub(/^\s*header "[^"\n]+-Swift\.h"\s*\n/, '')
    map.write(text) unless text == map.read
  end
  result << {name: name, sources: sources.size}
end

project.targets.each do |target|
  target.build_configurations.each do |config|
    config.build_settings['PODS_ROOT'] = overlay.to_s
    config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = cfg.fetch('deployment_target')
  end
  target.shell_script_build_phases.each do |phase|
    if cfg.fetch('prepared_phase_names', []).include?(phase.name)
      phase.shell_script = 'echo "Using independently prepared arm64 simulator libraries"'
    elsif phase.name.to_s.downcase == 'swiftlint'
      phase.shell_script = "if [ \"${SIMULATOR_SKIP_SWIFTLINT:-0}\" = \"1\" ]; then\n echo \"warning: SwiftLint NOT RUN: explicit simulator-only skip\"\nelse\n#{phase.shell_script}\nfi\n"
    end
  end
end
project.save
puts JSON.generate(source_targets: result, original_source_root: pods.to_s, overlay: overlay.to_s)
